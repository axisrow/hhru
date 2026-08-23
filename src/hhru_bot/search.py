from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import selectors as sel
from .browser import HH_BASE_URL, goto_hh
from .config import ResumeConfig, SearchFilters
from .config_sections.scoring import ScoringConfig, ScoringWeights
from .history import SKIP_REASONS
from .portfolio_evidence import PortfolioEvidenceRequirement, detect_portfolio_evidence

if TYPE_CHECKING:
    # EmployerInfo живёт в scoring.py; импортируем только для type-checking,
    # чтобы не создавать цикл search <-> scoring (scoring лениво тянет
    # search._score_card). На runtime нужен лишь конструктор (см. _parse_*).
    from .scoring import EmployerInfo

logger = logging.getLogger("hhru_bot.search")

# JS-страница поиска может отдать DOMContentLoaded до списка карточек. На
# живой выдаче #455 карточки второй страницы появились примерно через 20 с,
# поэтому прежние 10 с давали ложный ``VacancySearchIndeterminate``. Ждём
# ограниченно: 30 с заметно меньше навигационного потолка в 90 с и не
# превращает обход в бесконечное ожидание.
RENDER_TIMEOUT_MS = 30_000


class VacancySearchIndeterminate(RuntimeError):
    """Состояние выдачи или пагинации поиска не удалось подтвердить.

    Пустая выдача подтверждается отдельным empty-state-селектором. Timeout
    карточек/пагинации без него неотличим от устаревшего селектора или
    интерстишл-страницы и не должен молча выдаваться за «вакансий нет».
    """

    def __init__(
        self,
        message: str,
        *,
        state: str = "indeterminate",
        page_num: int | None = None,
        url: str | None = None,
        partial_results: list[VacancyCard] | None = None,
        diagnostics: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.state = state
        self.page_num = page_num
        self.url = url
        self.partial_results = list(partial_results or [])
        self.diagnostics = diagnostics or {}


def _search_diagnostics(page: Page, page_num: int, url: str) -> dict[str, object]:
    """Small, non-sensitive snapshot suitable for CLI/log output."""
    diagnostics: dict[str, object] = {"page": page_num, "url": url}
    try:
        diagnostics["current_url"] = page.url
    except Exception:  # noqa: BLE001 - diagnostics must never mask root failure
        diagnostics["current_url"] = None
    try:
        diagnostics["title"] = page.title()
    except Exception:  # noqa: BLE001
        diagnostics["title"] = None
    for name, selector in (
        ("card_count", sel.VACANCY_CARD),
        ("title_count", sel.VACANCY_CARD_TITLE_LINK),
        ("empty_count", sel.VACANCY_SEARCH_EMPTY),
    ):
        try:
            diagnostics[name] = page.locator(selector).count()
        except Exception:  # noqa: BLE001
            diagnostics[name] = None
    return diagnostics


def _search_state(page: Page) -> str:
    try:
        current_url = str(page.url)
    except Exception:  # noqa: BLE001
        current_url = ""
    if "/account/login" in current_url:
        return "unauthenticated"
    try:
        from .browser import has_login_form

        if has_login_form(page):
            return "unauthenticated"
    except Exception:  # noqa: BLE001
        pass
    return "indeterminate"


def _typed_search_failure(
    page: Page,
    *,
    state: str,
    page_num: int,
    url: str,
    results: list[VacancyCard],
    detail: str,
) -> VacancySearchIndeterminate:
    return VacancySearchIndeterminate(
        f"выдача поиска на странице {page_num} не подтверждена ({state}): {detail}",
        state=state,
        page_num=page_num,
        url=url,
        partial_results=results,
        diagnostics=_search_diagnostics(page, page_num, url),
    )


@dataclass
class SalaryInfo:
    """Распарсенная зарплата вакансии (issue #14).

    salary_from/salary_to могут быть None: диапазон «от N» → from=N, to=None;
    «до N» → from=None, to=N; «от A до B» → оба. Фиксированное значение N
    сохраняется как from=to=N (без диапазона). currency — ISO-код валюты
    (RUB/USD/EUR/KZT/BYN) либо исходная строка, если валюта не распознана.
    raw — оригинальный текст блока (для логов/диагностики).
    """

    salary_from: int | None
    salary_to: int | None
    currency: str
    raw: str


def parse_publication_time(text: str | None, now: datetime | None = None) -> datetime | None:
    """Parse hh.ru's relative or short Russian publication date."""
    if not text or not text.strip():
        return None
    now = now or datetime.now()
    value = " ".join(text.lower().strip().split())
    if "сегодня" in value or value == "today":
        return now
    if "вчера" in value or value == "yesterday":
        return now - timedelta(days=1)
    match = re.search(r"(\d+)\s+(?:день|дня|дней|day|days)\s+(?:назад|ago)", value)
    if match:
        return now - timedelta(days=int(match.group(1)))
    match = re.search(r"(\d{1,2})[.]\s*(\d{1,2})(?:[.]\s*(\d{2,4}))?", value)
    if match:
        year = int(match.group(3) or now.year)
        if year < 100:
            year += 2000
        try:
            return datetime(year, int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", value)
    if match and match.group(2) in months:
        try:
            return datetime(
                int(match.group(3) or now.year), months[match.group(2)], int(match.group(1))
            )
        except ValueError:
            return None
    return None


# --- парсер зарплаты (чистая функция, issue #14) ----------------------------
#
# hh.ru форматирует зарплату одним из способов:
#   «150 000–200 000 руб.»  — диапазон, рубли
#   «от 80 000 ₽»           — нижняя граница
#   «до 120 000 руб.»       — верхняя граница
#   «100 000 ₽»             — фиксированное значение
#   «3 000–5 000 USD»       — иностранная валюта
#   «з/п не указана»        — нет данных → None
# Разделители разрядов: обычный пробел, неразрывный U+00A0 и узкий U+202F.
# Дефис диапазона: обычный '-', en-dash '–' (U+2013), em-dash '—' (U+2014).

# Любой разделитель разрядов числа (включая неразрывные пробелы hh.ru).
_DIGIT_GROUP_SEP = r"[   ]"

_NUMBER = rf"\d+(?:{_DIGIT_GROUP_SEP}?\d{{3}})*"

# Карта: распознаваемое в тексте обозначение → ISO-код валюты.
# Порядок важен: более длинные/специфичные строки раньше коротких.
_CURRENCY_PATTERNS: list[tuple[str, str]] = [
    ("бел. руб", "BYN"),
    ("бел.руб", "BYN"),
    ("руб.", "RUB"),
    ("руб", "RUB"),
    ("₽", "RUB"),
    ("usd", "USD"),
    ("$", "USD"),
    ("eur", "EUR"),
    ("€", "EUR"),
    ("kzt", "KZT"),
    ("тг", "KZT"),
    ("₸", "KZT"),
    ("грн", "UAH"),
    ("₴", "UAH"),
]

# Заглушки hh.ru, означающие «зарплаты нет».
_NO_SALARY_MARKERS = ("з/п не указана", "зп не указана", "не указана", "по договоренности")


def _strip_digits(token: str) -> int:
    """Убирает разделители разрядов и возвращает int."""
    cleaned = re.sub(_DIGIT_GROUP_SEP, "", token)
    return int(cleaned)


def _detect_currency(text: str) -> str:
    """Возвращает ISO-код валюты или исходный короткий токен, если не распознан."""
    lower = text.lower()
    for needle, code in _CURRENCY_PATTERNS:
        if needle in lower:
            return code
    # Незнакомая валюта: вернём последний «словесный» токен (не число),
    # чтобы не терять информацию и не падать. Пусто → RUB по умолчанию.
    words = [w for w in re.split(r"[\s.,]+", text) if w and not w.isdigit()]
    if words:
        return words[-1]
    return "RUB"


def parse_salary(text: str | None) -> SalaryInfo | None:
    """Разбирает строку зарплаты hh.ru в SalaryInfo, либо None.

    Чистая функция (без браузера) — ради тестируемости. Принимает «грязный»
    текст блока компенсации и нормализует его. Возвращает None для заглушек
    «з/п не указана»/пустоты/None.
    """
    if not text:
        return None

    raw = text.strip()
    if not raw:
        return None

    if any(marker in raw.lower() for marker in _NO_SALARY_MARKERS):
        return None

    currency = _detect_currency(raw)
    numbers = re.findall(_NUMBER, raw)

    if not numbers:
        return None

    has_from = "от" in raw.lower()
    has_to = "до" in raw.lower()

    if len(numbers) >= 2:
        # Диапазон «A–B».
        salary_from = _strip_digits(numbers[0])
        salary_to = _strip_digits(numbers[1])
    elif has_from:
        salary_from = _strip_digits(numbers[0])
        salary_to = None
    elif has_to:
        salary_from = None
        salary_to = _strip_digits(numbers[0])
    else:
        # Фиксированное значение.
        salary_from = salary_to = _strip_digits(numbers[0])

    return SalaryInfo(salary_from=salary_from, salary_to=salary_to, currency=currency, raw=raw)


@dataclass
class VacancyCard:
    vacancy_id: str
    title: str
    company: str
    url: str
    # Зарплата (issue #14). None — если hh.ru не отдал блок или парсер
    # не смог разобрать (т.е. «з/п не указана»).
    salary: SalaryInfo | None = None
    published_at: datetime | None = None
    # Инфо о работодателе из карточки поиска (issue #74, Этап 1): рейтинг,
    # число отзывов, бейдж «надёжный работодатель». hh.ru показывает эти блоки
    # не для всех карточек → None/False по умолчанию. Источник факторов для
    # classify_employer и LLM-скоринга (#74 Этапы 2-3). Ленивый импорт типа —
    # чтобы не тащить scoring.py (а с ним ai) на уровень search.py импорта.
    employer_info: EmployerInfo | None = None
    # Read-only signal from vacancy/card text.  Full descriptions can be passed
    # by callers through ``vacancy_text`` when available.
    vacancy_text: str = ""
    portfolio_evidence_requirement: PortfolioEvidenceRequirement | None = None
    # Доп. признаки карточки для статистики/ML (issue #517, приоритет-1 из
    # #516). Все блоки опциональны в разметке hh.ru — пустая строка/False,
    # если карточка их не отдала (тот же паттерн, что salary/published_at).
    address: str = ""
    is_remote: bool = False
    # Категория опыта как есть из data-qa (напр. "between1And3"), пустая
    # строка если блок не найден. Не нормализуем в enum — источник истины
    # для дальнейшего анализа остаётся значение hh.ru.
    experience: str = ""
    snippet_requirement: str = ""
    snippet_responsibility: str = ""

    def __post_init__(self) -> None:
        if self.portfolio_evidence_requirement is None and self.vacancy_text:
            self.portfolio_evidence_requirement = detect_portfolio_evidence(self.vacancy_text)


def build_search_url(filters: SearchFilters, page_num: int = 0) -> str:
    params = {"text": filters.text, "page": page_num}
    if filters.area is not None:
        params["area"] = filters.area
    if filters.salary_from is not None:
        params["salary"] = filters.salary_from
    if filters.experience is not None:
        params["experience"] = filters.experience
    if filters.schedule is not None:
        params["schedule"] = filters.schedule
    return f"{HH_BASE_URL}/search/vacancy?{urlencode(params)}"


def _has_next_page(page: Page, page_num: int) -> bool:
    """Есть ли страница после ``page_num`` (issue #123).

    hh.ru отдаёт пагинацию в двух вариантах разметки (см. комментарий у
    ``PAGINATION_PAGE``): с кнопкой ``pager-next`` и без неё. Раньше признак
    брался только от ``pager-next``, поэтому во втором варианте сбор молча
    обрывался на первой странице — терялось до 90% выдачи.

    Признак берётся от пронумерованных ссылок ``pager-page``, которые есть в
    ОБОИХ вариантах: если среди них найден номер больше текущего (нумерация в
    UI с единицы, ``page_num`` с нуля), значит следующая страница существует.
    ``pager-next`` используется как дополнительный положительный сигнал — он
    достаточен, но не необходим.

    Нераспарсенные подписи (``...`` между номерами) игнорируются: они не несут
    номера страницы. Отсутствующий ``pager-block`` после готовой выдачи —
    подтверждённая единственная страница. Но если контейнер пагинации уже есть,
    а его страницы не появились за bounded timeout, поднимается
    :class:`VacancySearchIndeterminate`: такой DOM нельзя назвать последней
    страницей.
    """
    if page.locator(sel.PAGINATION_NEXT).count() > 0:
        return True

    pagination = page.locator(sel.PAGINATION_BLOCK)
    if pagination.count() == 0:
        return False

    pages = page.locator(sel.PAGINATION_PAGE)
    if pages.count() == 0:
        try:
            pages.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
        except PlaywrightError:
            raise VacancySearchIndeterminate(
                f"пагинация страницы поиска {page_num} не подтверждена: "
                f"маркер pager-page не появился за {RENDER_TIMEOUT_MS} мс"
            ) from None
        if pages.count() == 0:
            raise VacancySearchIndeterminate(
                f"пагинация страницы поиска {page_num} не подтверждена: "
                "pager-page исчез после ожидания"
            )

    for i in range(pages.count()):
        label = pages.nth(i).inner_text().strip()
        try:
            number = int(label)
        except ValueError:
            continue  # «...» и прочие не-номера
        if number > page_num + 1:
            return True
    return False


def search_vacancies(
    page: Page,
    filters: SearchFilters,
    max_pages: int = 5,
    start_page: int = 0,
) -> list[VacancyCard]:
    """
    Ищет вакансии по фильтрам, возвращает список карточек БЕЗ учёта
    exclude_employers/exclude_keywords и БЕЗ учёта истории откликов —
    эта фильтрация делается отдельно через filter_candidates(), чтобы
    её можно было протестировать и залогировать причины исключения.
    """
    results: list[VacancyCard] = []

    for page_num in range(start_page, start_page + max_pages):
        url = build_search_url(filters, page_num)
        logger.info("Загрузка страницы поиска: %s", url)
        cards = None
        # ``count()`` читает DOM немедленно, а выдача hh.ru появляется после
        # JS-рендера. Контейнер карточки может смонтироваться раньше её
        # содержимого, поэтому признак готовности — уже отрендеренная ссылка с
        # названием вакансии ИЛИ подтверждённый empty-state. ``wait_for`` не
        # делает фиксированную паузу: он возвращает управление сразу после
        # появления этого DOM-узла. Timeout остаётся только fail-closed
        # предохранителем для интерстишла/зависшей страницы.
        for render_attempt in range(2):
            try:
                goto_hh(page, url)
            except PlaywrightError as exc:
                if render_attempt == 0:
                    time.sleep(2)
                    continue
                raise _typed_search_failure(
                    page,
                    state="unreachable",
                    page_num=page_num,
                    url=url,
                    results=results,
                    detail=f"navigation failed after retry: {exc}",
                ) from None
            cards = page.locator(sel.VACANCY_CARD)
            ready = page.locator(f"{sel.VACANCY_CARD_TITLE_LINK}, {sel.VACANCY_SEARCH_EMPTY}")
            try:
                ready.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
                break
            except PlaywrightError:
                if render_attempt == 0:
                    logger.warning("Страница %d не отрендерилась; один bounded retry", page_num)
                    time.sleep(2)
                    continue
                state = _search_state(page)
                raise _typed_search_failure(
                    page,
                    state=state,
                    page_num=page_num,
                    url=url,
                    results=results,
                    detail=(
                        "ссылка вакансии или empty-state не появились "
                        f"за {RENDER_TIMEOUT_MS} мс после retry"
                    ),
                ) from None

        assert cards is not None

        count = cards.count()
        if count == 0:
            if page.locator(sel.VACANCY_SEARCH_EMPTY).count() > 0:
                logger.info("Страница %d: вакансий не найдено", page_num)
                break
            # Защита выше должна исключить этот путь для настоящего Locator;
            # сохраняем fail-closed для нестабильных/тестовых DOM-адаптеров.
            raise _typed_search_failure(
                page,
                state=_search_state(page),
                page_num=page_num,
                url=url,
                results=results,
                detail="после ожидания контейнер карточек пуст",
            )

        for i in range(count):
            card = cards.nth(i)
            card_text = card.inner_text()
            title_link = card.locator(sel.VACANCY_CARD_TITLE_LINK).first
            title = title_link.inner_text().strip()
            href = title_link.get_attribute("href") or ""
            vacancy_id = _extract_vacancy_id(href)

            # Контейнер карточки уже дождался рендера выше. Поля ниже опциональны,
            # поэтому отдельный wait_for на каждое из них избыточен: их отсутствие
            # не обрывает выдачу, а только оставляет конкретный атрибут пустым.
            company_locator = card.locator(sel.VACANCY_CARD_COMPANY).first
            company = company_locator.inner_text().strip() if company_locator.count() else ""

            # Зарплата (issue #14). Блок опционален: hh.ru не рендерит
            # compensation, если з/п не указана.
            # ЗП: сначала пробуем data-qa селектор, потом regex-fallback
            # по HTML карточки (hh.ru перешёл на magritte, #73).
            salary_text = _optional_text(card, sel.VACANCY_CARD_COMPENSATION)
            if salary_text is None:
                try:
                    # #93: textContent (inner_text) вместо innerHTML. Аудит показал,
                    # что hh.ru для части вакансий отдаёт ЗП только в textContent
                    # (JS-рендер в текстовый узел), а в innerHTML блока нет — регексп
                    # по innerHTML пропускал такие ЗП (5/15 → 19/20 ловится по тексту).
                    # extract_salary_text корректно работает и с HTML, и с голым текстом
                    # (удаление тегов на тексте = no-op), поэтому кормим её textContent.
                    salary_text = extract_salary_text(card_text)
                except Exception:
                    logger.warning("Не удалось получить inner_text для ЗП-fallback", exc_info=True)
            salary = parse_salary(salary_text)
            published_at = parse_publication_time(
                _optional_text(card, sel.VACANCY_CARD_PUBLICATION_TIME)
            )

            # Инфо о работодателе (issue #74, Этап 1): рейтинг/отзывы/trusted.
            # Блоки опциональны (не у всех работодателей). Парсим мягко — ни одно
            # поле не должно ронять сбор карточек. EmployerInfo импортируется
            # локально, чтобы не создавать цикл search <-> scoring на уровне
            # модуля (и не тянуть scoring/ai при import search).
            employer_info = _parse_employer_info(card)

            # Доп. признаки карточки для статистики/ML (issue #517). Все блоки
            # опциональны — мягкий парсинг, отсутствие не роняет сбор карточки.
            address = _optional_text(card, sel.VACANCY_CARD_ADDRESS) or ""
            is_remote = bool(card.locator(sel.VACANCY_CARD_REMOTE_LABEL).first.count())
            experience = _parse_experience(card)
            snippet_requirement = _optional_text(card, sel.VACANCY_CARD_SNIPPET_REQUIREMENT) or ""
            snippet_responsibility = (
                _optional_text(card, sel.VACANCY_CARD_SNIPPET_RESPONSIBILITY) or ""
            )

            if not vacancy_id:
                logger.warning("Не удалось извлечь vacancy_id из href='%s', пропуск", href)
                continue

            results.append(
                VacancyCard(
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    url=(
                        href.split("?")[0]
                        if href.startswith("http")
                        else f"{HH_BASE_URL}{href.split('?')[0]}"
                    ),
                    salary=salary,
                    published_at=published_at,
                    employer_info=employer_info,
                    # VacancyCard.__post_init__ derives portfolio_evidence_requirement
                    # from vacancy_text automatically; no need to compute it twice.
                    vacancy_text=card_text,
                    address=address,
                    is_remote=is_remote,
                    experience=experience,
                    snippet_requirement=snippet_requirement,
                    snippet_responsibility=snippet_responsibility,
                )
            )

        try:
            has_next = _has_next_page(page, page_num)
        except VacancySearchIndeterminate as exc:
            raise _typed_search_failure(
                page,
                state=exc.state,
                page_num=page_num,
                url=url,
                results=results,
                detail=str(exc),
            ) from None
        if not has_next:
            logger.info("Достигнута последняя страница поиска (%d)", page_num)
            break

    logger.info("Найдено вакансий всего: %d", len(results))
    return results


# --- извлечение ЗП из текста карточки (fallback при устаревшем data-qa, #73) ----

# Валидный диапазон зарплаты (issue #73). Отсекает ложные срабатывания
# на числах вроде «3000 отзывов» — но только если число стоит рядом с валютой.
# Нижняя граница 1 000 (покрывает USD/EUR/KZT), верхняя 50 000 000 (KZT).
_SALARY_MIN = 1_000
_SALARY_MAX = 50_000_000

# Шаблон: число с разделителями разрядов (>=3 цифры), опционально диапазон,
# затем валюта. currency_list = все строки из _CURRENCY_PATTERNS + символы.
_CURRENCY_ALT = r"(?:руб|руб\.|RUB|₽|USD|\$|EUR|€|KZT|тг|₸|BYN|бел\.?\s*руб|UAH|грн|₴)"

# Соответствует «150 000», «150 000–200 000», «от 80 000», «до 120 000».
_SALARY_PATTERN = re.compile(
    rf"(?:от\s+|до\s+)?{_NUMBER}(?:\s*[–—-]\s*{_NUMBER})?\s*{_CURRENCY_ALT}",
    re.IGNORECASE,
)


def _salary_value_in_range(raw: str) -> bool:
    """Проверяет, что хотя бы одно число в строке ЗП попадает в [_SALARY_MIN, _SALARY_MAX]."""
    numbers = re.findall(_NUMBER, raw)
    return any(_SALARY_MIN <= _strip_digits(n) <= _SALARY_MAX for n in numbers)


_TAG_RE = re.compile(r"<[^>]+>")


def extract_salary_text(text: str) -> str | None:
    """Извлекает текст ЗП из текста карточки regex'ом (fallback, issue #73/#93).

    Принимает ``textContent`` карточки (``card.inner_text()``) — НЕ сырой HTML
    (#93). Аудит показал: hh.ru для части вакансий отдаёт ЗП только в textContent
    (JS-рендер в текстовый узел), а в ``innerHTML`` блока нет — регексп по
    ``innerHTML`` пропускал такие ЗП. ``search_vacancies`` кормит эту функцию
    именно textContent, и ЗП ловится (5/15 → 19/20 по аудиту).

    Ради обратной совместимости функция также принимает сырую HTML (как #73/#78):
    ``_TAG_RE`` удаляет теги (на чистом тексте — no-op), и regex применяется к
    тексту без тегов. hh.ru (magritte) разбивает ЗП по spans: ``<span>150</span>
    <span> </span><span>000</span><span> </span><span>₽</span>`` — теги внутри
    числа и whitespace-only спаны разрывают «digits+валюта подряд», и регексп по
    сырой HTML не срабатывает. Удаляем теги и нормализуем пробелы — получаем
    «150 000 ₽», точно как ``card.inner_text()``.

    Фильтруем по валидному диапазону (``_salary_value_in_range``), чтобы отсечь
    ложные срабатывания ("3000 отзывов" и пр.).
    """
    text = _TAG_RE.sub("", text)
    match = _SALARY_PATTERN.search(text)
    if not match:
        return None
    salary_text = " ".join(match.group(0).split())
    if not _salary_value_in_range(salary_text):
        return None
    return salary_text


def extract_salary_text_from_html(html: str) -> str | None:
    """Deprecated-алиас для :func:`extract_salary_text` (#93).

    Историческое имя из #73/#78 — когда источник был ``innerHTML``. С #93
    источник сменился на ``inner_text`` (textContent), функция переименована в
    ``extract_salary_text`` (HTML теперь не предполагается). Алиас оставлен ради
    обратной совместимости существующих импортов/тестов и делегирует в новое имя.
    """
    return extract_salary_text(html)


_EXPERIENCE_PREFIX = "vacancy-serp__vacancy-work-experience-"


def _parse_experience(card) -> str:
    """Категория опыта работы (issue #517): значение закодировано в САМОМ
    суффиксе data-qa элемента (напр. "between1And3"), а не в его тексте —
    в отличие от остальных опциональных полей карточки. Блок опционален;
    отсутствие → пустая строка, не роняем сбор карточки.
    """
    locator = card.locator(sel.VACANCY_CARD_EXPERIENCE).first
    if not locator.count():
        return ""
    qa = locator.get_attribute("data-qa") or ""
    if not qa.startswith(_EXPERIENCE_PREFIX):
        return ""
    return qa[len(_EXPERIENCE_PREFIX) :]


def _optional_text(card, selector: str) -> str | None:
    """Возвращает .strip()-текст первого элемента по selector, либо None.

    Для опциональных блоков карточки (зарплата, дата): если элемента нет —
    None; пустая строка после strip тоже трактуется как None, чтобы парсер
    зарплаты не пытался разбирать пустоту.
    """
    locator = card.locator(selector).first
    if not locator.count():
        return None
    text = locator.inner_text().strip()
    return text or None


# --- парсинг инфо о работодателе из карточки (issue #74, Этап 1) -------------
#
# Селекторы подтверждены в DOM-дампе (read-only, 2026-07-28): rating-value,
# reviews-count, trusted-employer-link. Все три блока опциональны. Парсим
# мягко: число с разделителями разрядов (неразрывные пробелы), запятую как
# десятичный разделитель рейтинга. Любой сбой → None/False, не роняем сбор.


def _parse_employer_info(card) -> EmployerInfo | None:
    """Собирает EmployerInfo (рейтинг/отзывы/trusted) из карточки поиска (#74).

    Локальный импорт EmployerInfo — ради разрыва цикла search <-> scoring.
    Возвращает None, если ни одного блока работодателя в карточке нет (нет
    смысла хранить пустой info); иначе EmployerInfo с теми полями, что удалось
    распарсить (остальные — None/False).
    """
    from .scoring import EmployerInfo

    rating_text = _optional_text(card, sel.COMPANY_RATING_VALUE)
    reviews_text = _optional_text(card, sel.COMPANY_RATING_REVIEWS_COUNT)

    rating = _parse_rating(rating_text)
    reviews = _parse_reviews_count(reviews_text)
    trusted = bool(card.locator(sel.TRUSTED_EMPLOYER_LINK).first.count())

    if rating is None and reviews is None and not trusted:
        return None
    return EmployerInfo(rating=rating, reviews_count=reviews, trusted=trusted)


def _parse_rating(text: str | None) -> float | None:
    """Парсит «4,5» → 4.5; None/не-число → None. Запятая как десятичный разделитель."""
    if not text:
        return None
    cleaned = text.strip().replace(",", ".").split()[0]  # берём первый токен
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_reviews_count(text: str | None) -> int | None:
    """Парсит «245 отзывов» → 245 (с разделителями разрядов); None/нет числа → None."""
    if not text:
        return None
    numbers = re.findall(_NUMBER, text)
    if not numbers:
        return None
    try:
        return _strip_digits(numbers[0])
    except ValueError:
        return None


def _extract_vacancy_id(href: str) -> str | None:
    if not href:
        return None
    path = href.split("?")[0].rstrip("/")
    parts = path.split("/")
    return parts[-1] if parts and parts[-1].isdigit() else None


def filter_candidates(
    cards: list[VacancyCard],
    filters: SearchFilters,
    resume_id: str,
    history,
    prefilter_thresholds=None,
) -> tuple[list[VacancyCard], list[tuple[VacancyCard, str]]]:
    """
    Разделяет карточки на (подходящие, исключённые с причиной).
    Причины исключения: уже откликались ранее, попадание в стоп-лист, ИЛИ
    (опц.) непрохождение эвристического pre-LLM фильтра работодателя (#85).

    prefilter_thresholds (issue #85): опциональный PrefilterConfig. Если передан
    и enabled — после дедупа/стоп-листов применяется employer_passes_prefilter
    (чистая функция из scoring): карточки без сигнала работодателя (неизвестная
    компания, нет рейтинга/отзывов, не приглашал ранее) отсекаются ДО ранжирования
    и LLM-скоринга (#74) — экономия токенов. None / enabled=False = фильтр откл.
    (обратная совместимость, поведение не меняется). Применяется ПОСЛЕ дедупа/
    стоп-листов: «уже откликались» и стоп-слова — более определённые причины.

    #87: причины отсева пишутся в журнал ``skipped`` через ``history.record_skip``
    (стабильные enum-ключи SKIP_REASONS) — это кэш отсева: повторный search видит
    «уже отсеяна» и не пересматривает повторно (экономия LLM/времени #74/#85).
    ``is_skipped`` проверяется ПЕРВЫМ — если вакансия уже в кэше отсева, она не
    доходит ни до has_applied, ни до стоп-листов, и журнал не дублируется. Все
    ветки отсева (уже откликались / стоп-листы / pre-LLM фильтр #85) пишут свою
    причину — кэш консистентен по всем путям отсева.
    """
    from .scoring import employer_passes_prefilter  # локальный импорт: цикл search<->scoring

    candidates: list[VacancyCard] = []
    skipped: list[tuple[VacancyCard, str]] = []

    for card in cards:
        # #87 кэш отсева: вакансия уже отсеяна ранее — пропускаем без повторного
        # разбора (и без дублирующей записи в журнал). Экономит LLM/время.
        if history.is_skipped(resume_id, card.vacancy_id):
            skipped.append((card, "ранее отсеяна"))
            continue

        if history.has_applied(resume_id, card.vacancy_id):
            skipped.append((card, "уже откликались ранее"))
            history.record_skip(resume_id, card.vacancy_id, SKIP_REASONS.ALREADY_APPLIED)
            continue

        # Стоп-листы: employer и keyword — РАЗНЫЕ enum-причины, поэтому проверяем
        # каждую отдельно (раньше их сливала отдельная функция в одну строку).
        # Порядок employer→keyword сохранён. Совпадение берём конкретное — для
        # человекочитаемого вывода причины.
        company_lower = card.company.lower()
        employer_hit = next(
            (e for e in filters.exclude_employers if e.lower() in company_lower), None
        )
        if employer_hit is not None:
            skipped.append((card, f"компания в стоп-списке: {employer_hit}"))
            history.record_skip(resume_id, card.vacancy_id, SKIP_REASONS.STOPWORD_EMPLOYER)
            continue

        title_lower = card.title.lower()
        keyword_hit = next((k for k in filters.exclude_keywords if k.lower() in title_lower), None)
        if keyword_hit is not None:
            skipped.append((card, f"стоп-слово в названии: {keyword_hit}"))
            history.record_skip(resume_id, card.vacancy_id, SKIP_REASONS.STOPWORD_TITLE)
            continue

        # Pre-LLM фильтр работодателя (#85): отсев «слепых откликов» ДО скоринга.
        # None/disabled внутри функции = no-op (обратная совместимость). Отсеянная
        # здесь карточка пишется в skipped (#87) — кэш консистентен по всем путям
        # отсева, повторный search не пересматривает её.
        passes, prefilter_reason = employer_passes_prefilter(
            card, history, resume_id, prefilter_thresholds
        )
        if not passes:
            skipped.append((card, prefilter_reason))
            history.record_skip(resume_id, card.vacancy_id, SKIP_REASONS.LOW_EMPLOYER_SIGNAL)
            continue

        candidates.append(card)

    return candidates, skipped


# --- скоринг/ранжирование (issue #15) ---------------------------------------


def _tokenize(text: str) -> list[str]:
    """Простейшая токенизация в нижний регистр по не-буквенно-цифровым границам.

    Намеренно наивная: скоринг v1 работает по title/конфиг-ключевым словам,
    поэтому лемматизация/морфология не нужны — важна воспроизводимость и
    дешевизна (без внешних зависимостей).
    """
    tokens: list[str] = []
    current = ""
    for ch in text.lower():
        if ch.isalnum():
            current += ch
        elif current:
            tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    return tokens


_ZERO_WEIGHTS = ScoringWeights(
    must_have=0.0,
    nice_to_have=0.0,
    exclude_keyword=0.0,
    text_match=0.0,
)
"""Истинно нейтральные веса для legacy-конфига (без секции scoring).

Все факторы = 0 → score любой карточки = 0.0 → ранжирование не меняет
порядок входа (обратная совместимость candidates[:limit]). НЕ дефолтные
веса ScoringWeights(): там text_match=1.0 делал бы legacy score зависимым
от filters.text и нарушал бы совместимость, даже если must_have/nice_to_have
пусты. frozen-dataclass — переиспользуемая константа (immutable).
"""


def _score_card(
    card: VacancyCard,
    filters: SearchFilters,
    weights: ScoringWeights,
) -> tuple[float, dict[str, float]]:
    """Считает score карточки и разбивку по факторам.

    Факторы v1 (по доступным полям title/конфиг):
      - must_have: +weight за каждое must_have-слово, найденное в title (по токену).
      - nice_to_have: +weight за каждое nice_to_have-слово в title.
      - exclude_keyword: +weight (обычно отрицательный) за каждое стоп-слово в title.
      - text_match: +weight × долю токенов filters.text, найденных в title.

    exclude_keyword намеренно дублирует отсев filter_candidates: в нормальном
    потоке стоп-слова уже отсеяны до rank_candidates, но фактор оставлен для
    устойчивости (прямой вызов rank_candidates в обход фильтра, будущие
    изменения filter_candidates). v1 матчит ключевые слова по первому токену —
    рассчитано на однословные ключи (python/django); многословные ключи
    ('machine learning') совпадут по любому из своих токенов.
    """
    title_tokens = set(_tokenize(card.title))

    def _kw_in_title(kw: str) -> bool:
        kw_tokens = _tokenize(kw)
        return bool(kw_tokens) and kw_tokens[0] in title_tokens

    must_have_hits = sum(1 for kw in filters.must_have if _kw_in_title(kw))
    nice_hits = sum(1 for kw in filters.nice_to_have if _kw_in_title(kw))
    exclude_hits = sum(1 for kw in filters.exclude_keywords if _kw_in_title(kw))

    text_tokens = _tokenize(filters.text)
    if text_tokens:
        matched = sum(1 for t in text_tokens if t in title_tokens)
        text_ratio = matched / len(text_tokens)
    else:
        text_ratio = 0.0

    breakdown: dict[str, float] = {
        "must_have": weights.must_have * must_have_hits,
        "nice_to_have": weights.nice_to_have * nice_hits,
        "exclude_keyword": weights.exclude_keyword * exclude_hits,
        "text_match": weights.text_match * text_ratio,
    }
    return sum(breakdown.values()), breakdown


def rank_candidates(
    candidates: list[VacancyCard],
    filters: SearchFilters,
    resume: ResumeConfig,
    scoring_provider=None,
    *,
    llm_shortlist: int | None = None,
) -> list[tuple[VacancyCard, float, dict[str, float]]]:
    """Ранжирует кандидатов по убыванию score (issue #15, расширено #74).

    Чистая функция без браузера — ради тестируемости (как filter_candidates).
    Возвращает список (карточка, score, разбивка факторов), отсортированный
    по убыванию score; при равенстве — стабильно по ВХОДНОМУ порядку
    (сортировка только по score, стабильность Python-сорта сохраняет порядок).

    scoring_provider (issue #74): опциональный ScoringProvider (напр.
    LLMScoringProvider). Если передан — score/breakdown берутся из
    provider.score(card) (fallback внутри провайдера); иначе — эвристика
    #15 (``_score_card``). Обратная совместимость: provider=None = текущее
    поведение, ничего не меняется — тот же приём, что ``letter_provider`` в
    ApplyContext (#17). ai_profile (если есть) передаётся провайдеру как
    профиль кандидата.

    llm_shortlist (Codex #74 F3, анти-фрод): ограничивает число LLM-запросов.
    Когда задан (>0), сначала ВСЕ кандидаты скорятся дешёвой эвристикой
    (``_score_card``), сортируются, и провайдер (LLM) вызывается ТОЛЬКО на
    топ-K. Остальные сохраняют эвристический score. Так при dozens кандидатов
    число синхронных LLM-запросов = ≤ K, а не длина списка — критично, чтобы
    не выглядеть подозрительно для анти-фрода hh.ru и не раздувать стоимость.
    Без провайдера параметр игнорируется.

    Обратная совместимость: без scoring-конфига (и без provider) используются
    истинно нулевые веса (все факторы = 0), поэтому ВСЕ score = 0.0 и входной
    порядок сохраняется полностью — ranked[:limit] выбирает те же вакансии,
    что и старый candidates[:limit], дневной лимит уходит на тот же набор.
    """
    scoring: ScoringConfig | None = getattr(resume, "scoring", None)
    weights = scoring.weights if scoring is not None else _ZERO_WEIGHTS
    profile = getattr(resume, "ai_profile", None)

    # AI-путь без scoring-секции (#81, фикс Codex-ревью): при нейтральных весах
    # (_ZERO_WEIGHTS) все кандидаты tie на предранжировании → shortlist берёт
    # первые K по входному порядку, а не лучшие. LLM тогда скорит произвольные
    # первые K, а реально релевантные кандидаты за пределами K никогда не
    # displac'нутся → дневная квота уходит на неоптимальный набор. Поэтому для
    # AI-пути (provider передан) без явной scoring-секции предранжирование идёт
    # по дефолтным весам ScoringWeights() — top-K действительно лучшие, LLM
    # доранжирует релевантных. Legacy (provider=None) НЕ затрагивается: для него
    # веса по-прежнему _ZERO_WEIGHTS → входной порядок candidates[:limit] сохранён.
    if scoring is None and scoring_provider is not None:
        weights = ScoringWeights()

    # #492 Этап 1: keyword-соответствие профиля вакансии — ТОЛЬКО наблюдение.
    # Score не участвует в ранжировании и никого не отсеивает: сначала нужно
    # увидеть реальное распределение на живых прогонах, иначе порог Этапа 2
    # калибруется вслепую. Стоит до ветвления на shortlist, чтобы покрыть оба пути.
    _log_resume_match(candidates, profile)

    # Shortlist-режим (Codex #74 F3): предранжируем эвристикой, LLM — только топ-K.
    if scoring_provider is not None and llm_shortlist and llm_shortlist > 0:
        return _rank_with_llm_shortlist(
            candidates, filters, weights, profile, scoring_provider, llm_shortlist
        )

    scored: list[tuple[VacancyCard, float, dict[str, float]]] = []
    for card in candidates:
        if scoring_provider is not None:
            outcome = scoring_provider.score(card, profile)
            score = outcome.score_0_100
            breakdown = outcome.breakdown
        else:
            score, breakdown = _score_card(card, filters, weights)
        scored.append((card, score, breakdown))

    # Стабильно: сортировка только по score; равные score сохраняют входной
    # порядок (Timsort стабилен). Тай-брейк по vacancy_id намеренно убран —
    # он переупорядочивал бы legacy candidates[:limit] при перемешанных id.
    scored.sort(key=lambda item: -item[1])
    return scored


def _log_resume_match(candidates: list[VacancyCard], profile) -> None:
    """Логирует keyword-соответствие профиля каждой карточке (#492, Этап 1).

    Наблюдение без последствий: ничего не ранжирует и не отсеивает. Цель —
    накопить в логе реальное распределение значений, чтобы порог Этапа 2
    калибровался по факту, а не наугад.

    Профиль отсутствует или у карточек нет ``vacancy_text`` — логировать нечего,
    выходим молча. Импорт ленивый: тот же разрыв цикла search <-> scoring, что
    в ``filter_candidates``.

    **Известное ограничение выборки.** Все три вызова ``rank_candidates``
    (``commands/search.py``, ``commands/_common.py``, ``apply/router.py``) идут
    ПОСЛЕ ``filter_candidates``, поэтому в лог попадают только выжившие карточки
    — распределение смещено на величину отсева. Для калибровки порога Этапа 2
    это приемлемо: prefilter работодателя (#85) по умолчанию ВЫКЛЮЧЕН, а
    остальные причины отсева (уже откликались, стоп-слова, кэш ``skipped``) —
    решения, принятые не по смыслу вакансии, то есть не тот класс карточек,
    который порог Этапа 2 вообще должен судить. Если при калибровке выборки
    окажется мало, логирование переносится до ``filter_candidates`` — но там
    профиль сейчас недоступен без правки сигнатур, и ради Этапа 1 это лишнее.
    """
    if profile is None or not candidates:
        return

    from .scoring import resume_match_score  # локальный импорт: разрыв цикла

    for card in candidates:
        # Наблюдение не имеет права ронять план откликов: этот вызов стоит
        # внутри rank_candidates, поэтому необработанное исключение на одной
        # карточке оборвало бы весь apply — ради строчки в логе. Тот же приём,
        # что у остальных провайдеров скоринга (fallback внутри
        # LLMScoringProvider): сбой логируется, цикл идёт дальше.
        try:
            outcome = resume_match_score(card, profile)
        except Exception as exc:  # noqa: BLE001 — наблюдение не должно ронять apply
            logger.warning("resume-match failed for %s '%s': %s", card.vacancy_id, card.title, exc)
            continue
        logger.info(
            "resume-match %s '%s': %.1f/100 (%s)",
            card.vacancy_id,
            card.title,
            outcome.score_0_100,
            outcome.rationale,
        )


# Максимальный размер shortlist для LLM-скоринга по умолчанию (Codex #74 F3).
# Топ-10 эвристики — достаточно, чтобы дневной лимит откликов ушёл на лучшие
# совпадения, при этом число LLM-запросов ограничено десятью на батч.
_LLM_SHORTLIST_DEFAULT = 10


def _rank_with_llm_shortlist(
    candidates: list[VacancyCard],
    filters: SearchFilters,
    weights,
    profile,
    scoring_provider,
    shortlist_size: int,
) -> list[tuple[VacancyCard, float, dict[str, float]]]:
    """Предранжирование эвристикой → LLM только по топ-K (Codex #74 F3).

    1. Все кандидаты получают эвристический score (дёшево, без LLM/сети).
    2. Сортируются по эвристике; берётся топ-K (shortlist).
    3. К топ-K применяется провайдер (LLM); score/breakdown заменяются на его.
       Карточки вне shortlist сохраняют эвристический score.
    4. Финальная сортировка по итоговому score. Circuit-breaker провайдера
       дополнительно ограничивает LLM-запросы при деградации endpoint'а.
    """
    # Шаг 1: эвристический score для всех.
    heur: list[tuple[VacancyCard, float, dict[str, float]]] = []
    for card in candidates:
        score, breakdown = _score_card(card, filters, weights)
        heur.append((card, score, breakdown))

    # Шаг 2: топ-K по эвристике (стабильно по входному порядку при равенстве).
    heur.sort(key=lambda item: -item[1])
    shortlist_ids = {heur[i][0].vacancy_id for i in range(min(shortlist_size, len(heur)))}

    # Шаг 3: заменяем score у карточек shortlist на результат провайдера (LLM).
    scored: list[tuple[VacancyCard, float, dict[str, float]]] = []
    for card, heur_score, heur_breakdown in heur:
        if card.vacancy_id in shortlist_ids:
            outcome = scoring_provider.score(card, profile)
            scored.append((card, outcome.score_0_100, outcome.breakdown))
        else:
            scored.append((card, heur_score, heur_breakdown))

    # Шаг 4: финальная стабильная сортировка по итоговому score.
    scored.sort(key=lambda item: -item[1])
    return scored
