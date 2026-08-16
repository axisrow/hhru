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

import json
import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
)
from .selector_groups import negotiations as ns

logger = logging.getLogger("hhru_bot.responses")

NEGOTIATIONS_URL = f"{HH_BASE_URL}/applicant/negotiations"

# Ждём появления карточек на JS-рендеренной странице. Достаточно для типичного
# рендера hh.ru; если за это время карточек нет — считаем страницу пустой/селектор
# устаревшим (см. fetch_responses). Не бесконечно, чтобы обход не зависал.
RENDER_TIMEOUT_MS = 10_000


class NotAuthenticated(PageStateIndeterminate):
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
    если chat_url без topic (ответ без чата ЛИБО неоднозначная SSR-привязка —
    см. topic_ambiguous).

    topic_ambiguous различает эти два случая topic=None: True — SSR-состояние
    страницы содержало >1 topic-кандидата для этой вакансии, сопоставить с
    конкретной карточкой однозначно не удалось (см. fail-closed guard в
    fetch_responses); False (по умолчанию) — карточка легитимно без чата
    (напр. discard) ИЛИ topic успешно сопоставлен. history.upsert_response
    матчит существующую строку по ``(vacancy_id, topic IS NULL)``, поэтому
    несколько ambiguous-карточек одной вакансии персистились бы как одна и та
    же NULL-строка, сливая/перезаписывая разные переписки — commands/responses
    обязан пропускать upsert для topic_ambiguous=True карточек.
    """

    vacancy_id: str
    status: str
    employer: str = ""
    chat_url: str | None = None
    topic: str | None = None
    date: str = ""
    raw_status: str = ""
    topic_ambiguous: bool = False


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


def _optional_text(item, *selectors: str) -> str:
    """Текст первого найденного элемента карточки, либо пустая строка.

    Опциональные поля (работодатель, статус). Как search._optional_text, но без
    None → пустая строка (для responses пустота нормальна и удобнее в dataclass).
    Selectors are ordered live-first, with legacy markup as a fallback.
    """
    for selector in selectors:
        loc = item.locator(selector).first
        if loc.count():
            text = loc.inner_text().strip()
            if text:
                return text
    return ""


def _status_text(item, *selectors: str) -> str:
    """Return the first non-empty status from ordered selectors.

    A present live status is authoritative even when unrecognized by
    normalize_status() — only an EMPTY selector falls through to the next
    one. Falling through on "unrecognized" instead of "empty" would let a
    stale legacy status silently override a real (if unmapped) live status
    whenever both markup variants happen to be present in the same card.
    """
    for selector in selectors:
        text = _optional_text(item, selector)
        if text:
            return text
    return ""


def _first_locator(item, *selectors):
    """Return the first matching locator, allowing old cached markup fixtures."""
    for selector in selectors:
        loc = item.locator(selector).first
        if loc.count():
            return loc
    return item.locator(selectors[0]).first


def _href_or_ancestor_href(link) -> str:
    """Read ``href`` off ``link``, falling back to its nearest ``<a>`` ancestor.

    #44 live check (2026-08-16): ``negotiations-item-vacancy`` is a ``<span>``
    with no href of its own; the vacancy link lives on the wrapping ``<a>``.
    Reading href directly on the data-qa node silently returned "" for every
    card, so ``parse_response_card`` dropped the whole page ("vacancy_id не
    извлечён"). Prefer the element's own href when present (covers markup
    where data-qa IS the anchor, e.g. LEGACY_* fixtures), else walk up to the
    closest anchor ancestor — same category of DOM-structure assumption as
    ``_form_scope()`` in apply/questions.py, not yet independently reconfirmed
    beyond this one live check.
    """
    if not link.count():
        return ""
    href = link.get_attribute("href")
    if href:
        return href
    ancestor = link.locator("xpath=ancestor::a[1]")
    if ancestor.count():
        return ancestor.get_attribute("href") or ""
    return ""


def parse_response_card(item) -> ResponseItem | None:
    """Парсит один locator карточки переписки в ResponseItem, либо None.

    None — если из ссылки не удалось достать vacancy_id (битая/пустая карточка).
    Чистая относительно Playwright-locator'а: импортирует только типы селекторов.
    """
    link = _first_locator(item, ns.NEGOTIATION_VACANCY_LINK, ns.LEGACY_NEGOTIATION_VACANCY_LINK)
    vacancy_href = _href_or_ancestor_href(link)
    vacancy_id = _extract_vacancy_id(vacancy_href)
    if not vacancy_id:
        return None

    # Prefer confirmed live selectors consistently; old saved markup remains a
    # fallback for fixtures and previously captured pages.
    raw_status = _status_text(item, ns.NEGOTIATION_STATUS, ns.LEGACY_NEGOTIATION_STATUS)
    employer = _optional_text(item, ns.NEGOTIATION_EMPLOYER, ns.LEGACY_NEGOTIATION_EMPLOYER)
    date = _optional_text(item, ns.NEGOTIATION_DATE, ns.LEGACY_NEGOTIATION_DATE)

    chat_link = _first_locator(item, ns.NEGOTIATION_CHAT_LINK, ns.LEGACY_NEGOTIATION_CHAT_LINK)
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
        # negotiations нет проверенного empty-state, поэтому на первой странице
        # timeout сохраняет совместимый контракт честно пустого inbox; на
        # подтверждённой последующей странице он означает indeterminate.
        #
        # #142: почему НЕ ready_selector в goto_hh выше — тут нужна ДРУГАЯ семантика,
        # чем у goto_hh(ready_selector=...): (1) state="attached" (наличие в DOM, а не
        # visible — карточка может быть ниже сгиба); (2) короткий RENDER_TIMEOUT_MS
        # (10с, не 90с GOTO_TIMEOUT_MS — пустой inbox должен детектиться быстро);
        # (3) таймаут НЕ фатален — warning + трактуем как empty, тогда как goto_hh
        # с ready_selector рейзит (что для healthcheck/apply верно, а для read-only
        # сбора ответов уронило бы всю команду на genuinely-пустом ящике).
        cards_rendered = True
        try:
            page.locator(ns.NEGOTIATION_ITEM).first.wait_for(
                state="attached", timeout=RENDER_TIMEOUT_MS
            )
        except PlaywrightError:
            cards_rendered = False
            logger.warning(
                "Страница %d: карточки переписки не появились за %d мс — "
                "список пуст либо устарел селектор negotiations-item",
                page_num,
                RENDER_TIMEOUT_MS,
            )

        cards = page.locator(ns.NEGOTIATION_ITEM)
        count = cards.count()
        if count == 0:
            if page_num > 0 and not cards_rendered:
                raise ResponsesIndeterminate(
                    f"страницы {page_num} не подтверждена: карточки переписки "
                    f"не появились за {RENDER_TIMEOUT_MS} мс после подтверждённой пагинации"
                )
            logger.info("Страница %d: ответов не найдено, останавливаюсь", page_num)
            break

        page_start = len(results)
        for i in range(count):
            item = parse_response_card(cards.nth(i))
            if item is None:
                logger.debug(
                    "Страница %d, карточка %d: vacancy_id не извлечён, пропуск", page_num, i
                )
                continue
            results.append(item)

        # The live open_chat control is a button without href.  Recover the
        # stable topic from the same page's SSR state, preserving distinct
        # negotiations for one vacancy. Slice by page_start (count of items
        # actually appended this page), NOT by the DOM card count `count` —
        # parse_response_card can skip cards (missing vacancy_id), so `count`
        # overcounts and a `-count:` slice would reach into the previous
        # page's already-resolved results and risk assigning them this
        # page's SSR topics.
        try:
            from .negotiations_probe import chat_url, topic_refs

            if not hasattr(page, "content"):
                raise ValueError("page.content unavailable")
            refs = topic_refs(page.content())
            refs_by_vacancy: dict[str, list] = {}
            for ref in refs:
                refs_by_vacancy.setdefault(ref.vacancy_id or "", []).append(ref)
            # Fail-closed on ambiguity: pairing an SSR topic to a DOM card relies on
            # matching by vacancy_id alone, and there is no verified invariant that
            # DOM card order matches SSR topicList order (no live fixture with >1
            # topic per vacancy exists yet to confirm/refute it — same category as
            # the _form_scope() DOM-structure assumption in apply/questions.py).
            # Decide per vacancy_id GROUP up front, from counts only — never mutate
            # refs_by_vacancy (e.g. via candidates.pop()) while iterating cards.
            # Regression (#186 round 4): consuming the shared candidate list one
            # card at a time meant a second card for the same vacancy_id could see
            # an already-drained list (len 0) after an earlier card popped its sole
            # candidate — neither the "exactly one" nor the "more than one" branch
            # matched, so the second card silently kept topic_ambiguous=False and
            # was persisted as if it legitimately had no chat. Only attach when the
            # vacancy_id has exactly one card AND exactly one SSR candidate — the
            # only case that's unambiguous regardless of ordering. Any other
            # mismatch (more cards than candidates, more candidates than cards, or
            # multiple cards at all) marks every unresolved card for that
            # vacancy_id as ambiguous instead of guessing positionally.
            cards_by_vacancy: dict[str, list] = {}
            for result in results[page_start:]:
                if result.topic is None:
                    cards_by_vacancy.setdefault(result.vacancy_id, []).append(result)
            for vacancy_id, cards in cards_by_vacancy.items():
                candidates = refs_by_vacancy.get(vacancy_id, [])
                if len(cards) == 1 and len(candidates) == 1:
                    ref = candidates[0]
                    cards[0].topic = ref.topic_id
                    cards[0].chat_url = chat_url(ref.chat_id)
                elif candidates:
                    for card in cards:
                        card.topic_ambiguous = True
                    logger.warning(
                        "Отклик vacancy_id=%s: %d карточек и %d кандидатов SSR-topic "
                        "— сопоставление неоднозначно, topic не присвоен",
                        vacancy_id,
                        len(cards),
                        len(candidates),
                    )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError, PlaywrightError):
            logger.warning("SSR topic mapping unavailable; keeping parsed chat URLs")

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
