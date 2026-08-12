"""Мониторинг ответов работодателей (#12, Этап 2).

Владелец: #12. Не трогает apply/ и search.py — отдельный поток данных:
/applicant/negotiations → fetch_responses → history.upsert_response.

Поток: команда responses открывает страницу откликов/переписки, fetch_responses
собирает карточки переписок в ResponseItem (vacancy_id/работодатель/статус/
дата/ссылка на чат), команда upsert'ит их в историю и печатает ASCII-сводку
«что нового» (new_responses_since прошлой отметки).

Read-only по отношению к hh.ru: страница откликов только читается, никаких
кликов «ответить»/навигации в чат. Как и search.search_vacancies, перебор
страниц списка идёт без throttle-пауз (паузы применяются к ДЕЙСТВИЯМ apply/
bump, не к чтению списка); истёкшая сессия (редирект на страницу входа)
поднимает NotAuthenticated, чтобы команда не выдала пустой результат за «чисто».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form
from .selector_groups import negotiations as ns

logger = logging.getLogger("hhru_bot.responses")

NEGOTIATIONS_URL = f"{HH_BASE_URL}/applicant/negotiations"

# Ждём появления карточек на JS-рендеренной странице. Достаточно для типичного
# рендера hh.ru; если за это время карточек нет — считаем страницу пустой/селектор
# устаревшим (см. fetch_responses). Не бесконечно, чтобы обход не зависал.
RENDER_TIMEOUT_MS = 10_000


class NotAuthenticated(RuntimeError):
    """Сессия hh.ru истекла: страница переговоров не подтверждает auth-cookie.

    fetch_responses поднимает это, чтобы команда responses НЕ трактовала пустой
    результат выгруженной сессии как «нет новых ответов» и НЕ затирала историю.
    """


class ResponsesIndeterminate(RuntimeError):
    """Пагинация responses не успела подтвердить свой DOM.

    У negotiations нет проверенного empty-state-селектора: timeout карточек
    сохраняет исторический контракт пустого inbox, но неподтверждённый конец
    пагинации нельзя выдавать за последнюю страницу.
    """


# --- статусы ответов работодателя -------------------------------------------
# Нормализуем текст бейджа hh.ru в стабильный маркер. Это источник правды для
# storage (history.responses.status) и вывода команды. Чистая функция — ради
# тестируемости без браузера.
#
# Подмножество переходов (соответствует состояниям переписки hh.ru):
#   invitation — «Приглашение на собеседование» (работодатель позвал).
#   response   — «Ответ работодателя» / новое сообщение без приглашения.
#   discard    — «Отказ» (vacancy закрыта / отказали).
#   read       — «Прочитано» / нет активного действия (отклик просмотрен).
#   unknown    — незнакомый бейдж; храним как есть (не падаем) — пользователь
#                увидит сырой текст в выводе, БД хранит читаемую строку-ключ.


class ResponseStatus:
    """Стабильные строковые ключи статуса ответа работодателя."""

    INVITATION = "invitation"
    RESPONSE = "response"
    DISCARD = "discard"
    READ = "read"
    UNKNOWN = "unknown"


# Карта: подстрока текста бейджа (нижний регистр) → ключ статуса. Порядок важен:
# более специфичные («приглашени») раньше общих («прочитан»). Синонимы covers
# реальные формулировки hh.ru в шапке карточки переписки.
_STATUS_MAP: list[tuple[str, str]] = [
    ("приглашени", ResponseStatus.INVITATION),  # Приглашение / Приглашен(а)
    ("собеседован", ResponseStatus.INVITATION),
    ("отказ", ResponseStatus.DISCARD),  # Отказ / Отклонено
    ("отклонен", ResponseStatus.DISCARD),
    ("закрыт", ResponseStatus.DISCARD),  # «вакансия закрыта»
    ("новое сообщен", ResponseStatus.RESPONSE),  # новое сообщение от работодателя
    ("ответил", ResponseStatus.RESPONSE),
    ("ответ от", ResponseStatus.RESPONSE),
    ("непрочитан", ResponseStatus.RESPONSE),  # есть непрочитанное — значит ответили
    ("прочитан", ResponseStatus.READ),  # Прочитано / прочитан(а)
    ("просмотрен", ResponseStatus.READ),
]


def normalize_status(text: str | None) -> str:
    """Текст бейджа hh.ru → стабильный ключ статуса (ResponseStatus.*) или ``unknown``.

    Чистая функция. None/пусто → ``read`` (свежий отклик без явного бейджа
    трактуем как «прочитан / ждёт ответа» — нейтральное состояние, не «новый
    ответ работодателя»). Незнакомый текст → ``unknown`` с сохранением исходной
    строки в логах (через caller), сам ключ короткий для storage.
    """
    if not text:
        return ResponseStatus.READ
    lower = text.strip().lower()
    if not lower:
        return ResponseStatus.READ
    for needle, key in _STATUS_MAP:
        if needle in lower:
            return key
    return ResponseStatus.UNKNOWN


@dataclass
class ResponseItem:
    """Один ответ работодателя из /applicant/negotiations.

    status — стабильный ключ (ResponseStatus.*), не сырой текст hh.ru;
    employer/chat_url/date могут быть пустыми (hh.ru прячет компанию для части
    вакансий, чата нет при отказе, дата рендерится не всегда). raw_status —
    оригинальный текст бейджа для вывода/диагностики. topic — идентификатор
    переписки (из chat_url ?topic=...), уникальный для конкретного чата; None,
    если chat_url без topic (ответ без чата).
    """

    vacancy_id: str
    status: str
    employer: str = ""
    chat_url: str | None = None
    topic: str | None = None
    date: str = ""
    raw_status: str = ""


def _extract_vacancy_id(href: str) -> str | None:
    """Достаёт числовой vacancy_id из href ссылки вакансии/чата.

    Ссылки hh.ru на этой странице: ``/vacancy/12345?...``,
    ``/applicant/vacancy/12345?...``, чат ``/applicant/negotiations?...&vacancyId=12345``.
    Числовой tail пути — приоритет; иначе query-параметр vacancyId.
    """
    if not href:
        return None
    path, _, query = href.partition("?")
    tail = path.rstrip("/").split("/")[-1]
    if tail.isdigit():
        return tail
    # Fallback: vacancyId в query (чат-ссылки).
    m = re.search(r"(?:^|&)vacancyId=(\d+)", query)
    if m:
        return m.group(1)
    return None


def _extract_topic(chat_url: str | None) -> str | None:
    """Достаёт topic (идентификатор переписки) из chat_url, либо None.

    chat_url чата hh.ru: ``/applicant/negotiations?topic=77&vacancyId=...`` —
    ``topic`` уникален для конкретной переписки (одна вакансия может дать
    НЕСКОЛЬКО переписок, напр. при отклике с разных резюме). None — если chat_url
    без topic (ответ без чата, напр. discard; fallback на карточку вакансии).
    """
    if not chat_url:
        return None
    _, _, query = chat_url.partition("?")
    m = re.search(r"(?:^|&)topic=(\d+)", query)
    return m.group(1) if m else None


def _absolute_url(href: str, *, keep_query: bool = False) -> str:
    """Делает href абсолютным (как в search.py): http... иначе prepend HH_BASE_URL.

    По умолчанию query-строка срезается (для ссылки вакансии ``/vacancy/123?from=
    serp`` → чистый URL вакансии, как в search._absolute_url). ``keep_query=True`` —
    для chat-ссылки ``/applicant/negotiations?topic=77``: topic определяет
    конкретную переписку, без него ссылка ведёт в общий список, а не в чат.
    """
    if keep_query:
        if href.startswith("http"):
            return href
        return f"{HH_BASE_URL}{href}"
    if href.startswith("http"):
        return href.split("?")[0]
    return f"{HH_BASE_URL}{href.split('?')[0]}"


def _optional_text(item, selector: str) -> str:
    """Текст первого элемента карточки по selector, либо пустая строка.

    Опциональные поля (работодатель, статус). Как search._optional_text, но без
    None → пустая строка (для responses пустота нормальна и удобнее в dataclass).
    """
    loc = item.locator(selector).first
    if not loc.count():
        return ""
    text = loc.inner_text().strip()
    return text or ""


def parse_response_card(item) -> ResponseItem | None:
    """Парсит один locator карточки переписки в ResponseItem, либо None.

    None — если из ссылки не удалось достать vacancy_id (битая/пустая карточка).
    Чистая относительно Playwright-locator'а: импортирует только типы селекторов.
    """
    link = item.locator(ns.NEGOTIATION_VACANCY_LINK).first
    vacancy_href = link.get_attribute("href") or "" if link.count() else ""
    vacancy_id = _extract_vacancy_id(vacancy_href)
    if not vacancy_id:
        return None

    raw_status = _optional_text(item, ns.NEGOTIATION_STATUS)
    employer = _optional_text(item, ns.NEGOTIATION_EMPLOYER)
    date = _optional_text(item, ns.NEGOTIATION_DATE)

    chat_link = item.locator(ns.NEGOTIATION_CHAT_LINK).first
    chat_href = chat_link.get_attribute("href") or "" if chat_link.count() else ""
    # chat_url — на страницу чата; keep_query=True: topic определяет конкретную
    # переписку, без него ссылка ведёт в общий список. Если отдельной чат-ссылки
    # нет (напр. discard) — fallback на карточку вакансии (query там не нужен).
    chat_url = (
        _absolute_url(chat_href, keep_query=True) if chat_href else _absolute_url(vacancy_href)
    )
    # topic — идентификатор переписки из chat_url (?topic=...); уникален для
    # конкретного чата (одна вакансия → несколько переписок). None, если chat_url
    # без topic (fallback на карточку вакансии / ответ без чата).
    topic = _extract_topic(chat_url)

    return ResponseItem(
        vacancy_id=vacancy_id,
        status=normalize_status(raw_status),
        employer=employer,
        chat_url=chat_url,
        topic=topic,
        date=date,
        raw_status=raw_status,
    )


def fetch_responses(page: Page, max_pages: int = 5) -> list[ResponseItem]:
    """Собирает ответы работодателей с /applicant/negotiations.

    Возвращает список ResponseItem (без дедупликации — upsert в истории её сделает
    по UNIQUE (vacancy_id, topic)). Пагинация: до ``max_pages``, стоп только на
    подтверждённой последней странице. Неподтверждённая пагинация поднимает
    :class:`ResponsesIndeterminate`, а не выдаёт неопределённость за последнюю
    страницу.

    Read-only по hh.ru: только goto + чтение, никаких кликов действий.

    Защита от ложного «пустого inbox»: страница рендерится JS, поэтому после
    DOMContentLoaded ждём bounded таймаут появления карточки (RENDER_TIMEOUT_MS),
    а не считаем count() сразу. Истёкшая сессия hh.ru может оставить hhtoken в
    cookie jar, поэтому после навигации дополнительно проверяем подтверждённый
    DOM-маркер серверной формы входа. Если он обнаружен, поднимается
    NotAuthenticated (команда НЕ должна трактовать такой пустой результат как
    «нет новых ответов» и НЕ должна затирать историю). Для списка negotiations пока нет проверенного
    empty-state-селектора: timeout логируется и сохраняет исторический контракт
    пустого inbox; fail-closed применяется к пагинации, где ложный конец теряет
    уже прочитанные карточки.
    """
    results: list[ResponseItem] = []

    for page_num in range(max_pages):
        url = NEGOTIATIONS_URL if page_num == 0 else f"{NEGOTIATIONS_URL}?page={page_num}"
        logger.info("Загрузка страницы откликов: %s", url)
        goto_hh(page, url)

        # Проверяем единый auth-маркер до чтения DOM: пустая страница без cookie
        # не должна маскироваться под пустой inbox.
        if not has_auth_cookie(page):
            raise NotAuthenticated(
                "cookie hhtoken не найден — сессия истекла (запустите `login`, затем повторите)"
            )

        # A stale/revoked hhtoken can remain in the jar.  The login form is a
        # server-rendered positive indication that hh.ru rejected the session;
        # URL checks are deliberately excluded because they rejected valid
        # sessions in #140/#145.
        if has_login_form(page):
            raise NotAuthenticated(
                "страница содержит форму входа при наличии hhtoken — сессия отвергнута "
                "сервером (запустите `login`, затем повторите)"
            )

        # DOMContentLoaded приходит раньше JS-карточек. Перед count() ждём attached
        # ограниченное время: так delayed-render карточки попадают в обход. У
        # negotiations нет проверенного empty-state, поэтому timeout сохраняет
        # совместимый контракт честно пустого inbox и логируется.
        #
        # #142: почему НЕ ready_selector в goto_hh выше — тут нужна ДРУГАЯ семантика,
        # чем у goto_hh(ready_selector=...): (1) state="attached" (наличие в DOM, а не
        # visible — карточка может быть ниже сгиба); (2) короткий RENDER_TIMEOUT_MS
        # (10с, не 90с GOTO_TIMEOUT_MS — пустой inbox должен детектиться быстро);
        # (3) таймаут НЕ фатален — warning + трактуем как empty, тогда как goto_hh
        # с ready_selector рейзит (что для healthcheck/apply верно, а для read-only
        # сбора ответов уронило бы всю команду на genuinely-пустом ящике).
        try:
            page.locator(ns.NEGOTIATION_ITEM).first.wait_for(
                state="attached", timeout=RENDER_TIMEOUT_MS
            )
        except PlaywrightError:
            logger.warning(
                "Страница %d: карточки переписки не появились за %d мс — "
                "список пуст либо устарел селектор negotiations-item",
                page_num,
                RENDER_TIMEOUT_MS,
            )

        cards = page.locator(ns.NEGOTIATION_ITEM)
        count = cards.count()
        if count == 0:
            logger.info("Страница %d: ответов не найдено, останавливаюсь", page_num)
            break

        for i in range(count):
            item = parse_response_card(cards.nth(i))
            if item is None:
                logger.debug(
                    "Страница %d, карточка %d: vacancy_id не извлечён, пропуск", page_num, i
                )
                continue
            results.append(item)

        if not _has_next_page(page, page_num):
            logger.info("Достигнута последняя страница откликов (%d)", page_num)
            break

    logger.info("Собрано ответов работодателей всего: %d", len(results))
    return results


def _has_next_page(page: Page, page_num: int) -> bool:
    """Подтверждённо ли существует следующая страница negotiations.

    Отсутствующий ``pager-block`` после готовых карточек означает единственную
    страницу. Если контейнер есть, ``pager-next`` достаточен; иначе проверяем
    нумерованные страницы и ждём их bounded-временем fail-closed.
    """
    if page.locator(ns.NEGOTIATIONS_PAGINATION_NEXT).count() > 0:
        return True

    pagination = page.locator(ns.NEGOTIATIONS_PAGINATION_BLOCK)
    if pagination.count() == 0:
        return False

    pages = page.locator(ns.NEGOTIATIONS_PAGINATION_PAGE)
    if pages.count() == 0:
        try:
            pages.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
        except PlaywrightError:
            raise ResponsesIndeterminate(
                f"пагинация ответов на странице {page_num} не подтверждена: "
                f"маркер pager-page не появился за {RENDER_TIMEOUT_MS} мс"
            ) from None
        if pages.count() == 0:
            raise ResponsesIndeterminate(
                f"пагинация ответов на странице {page_num} не подтверждена: "
                "pager-page исчез после ожидания"
            )

    for i in range(pages.count()):
        try:
            if int(pages.nth(i).inner_text().strip()) > page_num + 1:
                return True
        except ValueError:
            continue
    return False
