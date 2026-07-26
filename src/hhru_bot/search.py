from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import Page

from . import selectors as sel
from .browser import HH_BASE_URL
from .config import ResumeConfig, SearchFilters
from .config_sections.scoring import ScoringConfig, ScoringWeights

logger = logging.getLogger("hhru_bot.search")


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
    # Зарплата/дата (issue #14). None — если hh.ru не отдал блок или парсер
    # не смог разобрать (т.е. «з/п не указана»). raw_date — оригинальный текст
    # блока даты, как есть; парсинг конкретной даты в этом ишью не делается
    # (форматы hh.ru зависят от локали и не нужны для вывода поиска).
    salary: SalaryInfo | None = None
    raw_date: str | None = None


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


def _matches_exclusions(card: VacancyCard, filters: SearchFilters) -> str | None:
    """Возвращает причину исключения вакансии, либо None если она подходит."""
    company_lower = card.company.lower()
    for excluded in filters.exclude_employers:
        if excluded.lower() in company_lower:
            return f"компания в стоп-списке: {excluded}"

    title_lower = card.title.lower()
    for keyword in filters.exclude_keywords:
        if keyword.lower() in title_lower:
            return f"стоп-слово в названии: {keyword}"

    return None


def search_vacancies(
    page: Page,
    filters: SearchFilters,
    max_pages: int = 5,
) -> list[VacancyCard]:
    """
    Ищет вакансии по фильтрам, возвращает список карточек БЕЗ учёта
    exclude_employers/exclude_keywords и БЕЗ учёта истории откликов —
    эта фильтрация делается отдельно через filter_candidates(), чтобы
    её можно было протестировать и залогировать причины исключения.
    """
    results: list[VacancyCard] = []

    for page_num in range(max_pages):
        url = build_search_url(filters, page_num)
        logger.info("Загрузка страницы поиска: %s", url)
        page.goto(url, wait_until="domcontentloaded")

        cards = page.locator(sel.VACANCY_CARD)
        count = cards.count()
        if count == 0:
            logger.info("Страница %d: вакансий не найдено, останавливаюсь", page_num)
            break

        for i in range(count):
            card = cards.nth(i)
            title_link = card.locator(sel.VACANCY_CARD_TITLE_LINK).first
            title = title_link.inner_text().strip()
            href = title_link.get_attribute("href") or ""
            vacancy_id = _extract_vacancy_id(href)

            company_locator = card.locator(sel.VACANCY_CARD_COMPANY).first
            company = company_locator.inner_text().strip() if company_locator.count() else ""

            # Зарплата и дата публикации (issue #14). Блоки опциональны: hh.ru
            # не рендерит compensation, если з/п не указана; date всегда есть,
            # но на ошибку селектора реагируем мягко (raw_date=None), чтобы
            # сбор карточек не падал целиком из-за одной страницы.
            salary_text = _optional_text(card, sel.VACANCY_CARD_COMPENSATION)
            salary = parse_salary(salary_text)
            raw_date = _optional_text(card, sel.VACANCY_CARD_DATE)

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
                    raw_date=raw_date,
                )
            )

        next_button = page.locator(sel.PAGINATION_NEXT)
        if next_button.count() == 0:
            logger.info("Достигнута последняя страница поиска (%d)", page_num)
            break

    logger.info("Найдено вакансий всего: %d", len(results))
    return results


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
) -> tuple[list[VacancyCard], list[tuple[VacancyCard, str]]]:
    """
    Разделяет карточки на (подходящие, исключённые с причиной).
    Причины исключения: уже откликались ранее ИЛИ попадание в стоп-лист.
    """
    candidates: list[VacancyCard] = []
    skipped: list[tuple[VacancyCard, str]] = []

    for card in cards:
        if history.has_applied(resume_id, card.vacancy_id):
            skipped.append((card, "уже откликались ранее"))
            continue

        reason = _matches_exclusions(card, filters)
        if reason:
            skipped.append((card, reason))
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
) -> list[tuple[VacancyCard, float, dict[str, float]]]:
    """Ранжирует кандидатов по убыванию score (issue #15).

    Чистая функция без браузера — ради тестируемости (как filter_candidates).
    Возвращает список (карточка, score, разбивка факторов), отсортированный
    по убыванию score; при равенстве — стабильно по ВХОДНОМУ порядку
    (сортировка только по score, стабильность Python-сорта сохраняет порядок).

    Обратная совместимость: без scoring-конфига используются истинно нулевые
    веса (все факторы = 0), поэтому ВСЕ score = 0.0 и входной порядок
    сохраняется полностью — ranked[:limit] выбирает те же вакансии, что и
    старый candidates[:limit], дневной лимит уходит на тот же набор.
    """
    scoring: ScoringConfig | None = getattr(resume, "scoring", None)
    weights = scoring.weights if scoring is not None else _ZERO_WEIGHTS

    scored: list[tuple[VacancyCard, float, dict[str, float]]] = []
    for card in candidates:
        score, breakdown = _score_card(card, filters, weights)
        scored.append((card, score, breakdown))

    # Стабильно: сортировка только по score; равные score сохраняют входной
    # порядок (Timsort стабилен). Тай-брейк по vacancy_id намеренно убран —
    # он переупорядочивал бы legacy candidates[:limit] при перемешанных id.
    scored.sort(key=lambda item: -item[1])
    return scored
