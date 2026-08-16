"""#207: внешний верификатор вердикта apply по списку откликов.

Клик по кнопке отклика на странице вакансии — точка, после которой локальная
картина перестаёт быть достоверной: навигация к форме может не подтвердиться,
форма — не отрисоваться, success-сигнал — не пойматься, но отклик при этом
реально уходит на hh.ru (кейсы #199/МТС и #207/YADRO — оба отклика
подтверждены внешне при ``[FAIL]`` в CLI). Любой таймаут этой зоны,
финализированный как ``failed``/ранний выход, даёт false negative:
``has_applied()`` не видит ``failed`` → повторный отклик вторым письмом,
метрики недосчитывают.

Источник истины здесь — сама страница /applicant/negotiations (SSR-состояние
``HH-Lux-InitialState`` с ``topicList[].vacancyId`` — тот же читатель, что у
responses/reply-employers; DOM-карточки — fallback при недоступности SSR).
Read-only: только goto + чтение. Три исхода:

* ``found`` — вакансия присутствует в списке → отклик точно ушёл;
* ``not_found`` — список подтверждённо прочитан (SSR распарсен либо карточки
  отрендерились) и вакансии нет → отклик точно не ушёл;
* ``indeterminate`` — список достоверно не прочитан (сессия, рендер, goto) →
  вердикт остаётся за fail-closed-логикой pipeline (uncertain).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import goto_hh, has_auth_cookie, has_login_form
from ..negotiations_probe import parse_initial_state
from ..responses import (
    NEGOTIATIONS_URL,
    RENDER_TIMEOUT_MS,
    ResponsesIndeterminate,
    _has_next_page,
    parse_response_card,
)
from ..selector_groups import negotiations as ns
from .steps import _dump_navigation_diagnostics

logger = logging.getLogger("hhru_bot.apply.verify")

#: Polling-окно внешней проверки: попытки с интервалом. Отклик попадает в
#: topicList синхронно с submit, поэтому ретраи страхуют не «появление», а
#: отваливающиеся под DDoS-Guard загрузки списка (goto_hh внутри тоже ретраит).
NEGOTIATIONS_VERIFY_ATTEMPTS = 2
NEGOTIATIONS_VERIFY_POLL_INTERVAL_MS = 10_000
#: Сканируем страницу 0 и, если пагинатор подтверждает продолжение, страницу 1:
#: список отсортирован по свежести, только что отправленный отклик был бы на
#: странице 0 — глубокий скан не нужен.
NEGOTIATIONS_VERIFY_MAX_PAGES = 2

#: Тип инъекции в pipeline (как probe/letter_provider): page, vacancy_id,
#: resume_id → вердикт. None у контекста = проверка выключена (юнит-тесты).
ResponseVerifier = Callable[..., "NegotiationsVerifyResult"]

FOUND = "found"
NOT_FOUND = "not_found"
INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class NegotiationsVerifyResult:
    status: str
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.status == FOUND

    @property
    def indeterminate(self) -> bool:
        return self.status == INDETERMINATE


def verify_response_in_negotiations(
    page: Page, vacancy_id: str | None, resume_id: str | None = None
) -> NegotiationsVerifyResult:
    """Проверяет, присутствует ли вакансия в /applicant/negotiations.

    «Подтверждённо прочитанный» список (хотя бы одна чистая попытка из
    NEGOTIATIONS_VERIFY_ATTEMPTS) без вакансии — это ``not_found``, а не
    «не знаем»: серверный topicList — тот же источник, из которого hh.ru
    рисует карточки. Ни одной чистой попытки — ``indeterminate``.
    """
    if not vacancy_id:
        return NegotiationsVerifyResult(INDETERMINATE, "vacancy_id карточки неизвестен")
    wanted = str(vacancy_id)
    clean_read = False
    problem = "список откликов не прочитан"
    for attempt in range(1, NEGOTIATIONS_VERIFY_ATTEMPTS + 1):
        logger.info(
            "[VERIFY] попытка %d/%d: /applicant/negotiations, vacancy_id=%s",
            attempt,
            NEGOTIATIONS_VERIFY_ATTEMPTS,
            wanted,
        )
        try:
            goto_hh(page, NEGOTIATIONS_URL)
        except PlaywrightError as exc:
            problem = f"goto списка откликов не прошёл ({exc})"
            logger.warning("[VERIFY] %s", problem)
        else:
            # Истёкшая сессия не лечится ретраями: hhtoken мог остаться в jar,
            # но сервер отвечает формой входа (тот же маркер, что в fetch_responses).
            if not has_auth_cookie(page) or has_login_form(page):
                return _indeterminate(
                    page, wanted, "сессия не авторизована — список откликов недоступен"
                )
            found_detail, clean, page_problem = _scan_negotiations(page, wanted, resume_id)
            if found_detail is not None:
                logger.info("[VERIFY] отклик подтверждён: %s", found_detail)
                return NegotiationsVerifyResult(FOUND, found_detail)
            clean_read = clean_read or clean
            if page_problem:
                problem = page_problem
        if attempt < NEGOTIATIONS_VERIFY_ATTEMPTS:
            page.wait_for_timeout(NEGOTIATIONS_VERIFY_POLL_INTERVAL_MS)
    if clean_read:
        logger.info("[VERIFY] список прочитан, vacancy_id=%s отсутствует", wanted)
        return NegotiationsVerifyResult(NOT_FOUND, "список откликов прочитан, вакансии нет")
    return _indeterminate(page, wanted, problem)


def _indeterminate(page: Page, vacancy_id: str, detail: str) -> NegotiationsVerifyResult:
    # Дамп в стиле #195: неразобравшийся случай должен оставлять артефакты
    # для посмертной диагностики (селектор — первый подозреваемый, CLAUDE.md).
    _dump_navigation_diagnostics(page, "verify_indeterminate", vacancy_id)
    return NegotiationsVerifyResult(INDETERMINATE, detail)


def _scan_negotiations(
    page: Page, wanted: str, resume_id: str | None
) -> tuple[str | None, bool, str | None]:
    """Сканирует страницу 0 (+ следующую при подтверждённой пагинации).

    Возвращает (detail найденной темы | None, было ли чистое чтение,
    описание проблемы чтения | None).
    """
    clean = False
    problem: str | None = None
    for page_num in range(NEGOTIATIONS_VERIFY_MAX_PAGES):
        if page_num > 0:
            try:
                goto_hh(page, f"{NEGOTIATIONS_URL}?page={page_num}")
            except PlaywrightError as exc:
                problem = f"goto страницы {page_num} списка не прошёл ({exc})"
                break
        found_detail, page_clean, page_problem = _scan_single_page(page, wanted, resume_id)
        if page_problem:
            problem = page_problem
        if found_detail is not None:
            return found_detail, True, None
        clean = clean or page_clean
        if not _has_next_page_confirmed(page, page_num):
            break
    return None, clean, problem


def _scan_single_page(
    page: Page, wanted: str, resume_id: str | None
) -> tuple[str | None, bool, str | None]:
    try:
        html = page.content()
    except PlaywrightError as exc:
        return None, False, f"page.content() упал ({exc})"
    topics = _ssr_topic_list(html)
    if topics is not None:
        # SSR — серверная истина; DOM читает те же данные, fallback не нужен.
        for topic in topics:
            if str(topic.get("vacancyId", "")) == wanted:
                return _describe_topic(topic, resume_id), True, None
        return None, True, None
    dom_ids, cards_seen = _read_dom_vacancy_ids(page)
    if wanted in dom_ids:
        return "DOM-карточка списка (SSR-состояние недоступно)", True, None
    if cards_seen:
        return None, True, None
    return None, False, "список не отрендерился (нет ни SSR-состояния, ни карточек)"


def _describe_topic(topic: dict[str, Any], resume_id: str | None) -> str:
    detail = f"topic={topic.get('id')}" if topic.get("id") is not None else "topic=?"
    topic_resume = topic.get("resumeId")
    if topic_resume is not None:
        detail += f", resumeId={topic_resume}"
        if resume_id and str(topic_resume) != str(resume_id):
            detail += " (отклик ушёл с ДРУГОГО резюме)"
    return detail


def _ssr_topic_list(html: str) -> list[dict[str, Any]] | None:
    """topicList из SSR-состояния; None — состояние недоступно (не «пусто»).

    Сканирует сырые темы, а не topic_refs(): для проверки достаточно
    vacancyId, и политика дропа записей без id/chatId (для маппинга чатов)
    не должна превращать существующий отклик в «не найден».
    """
    try:
        state = parse_initial_state(html)
    except (ValueError, AttributeError):
        return None
    topics = state.get("applicantNegotiations", {}).get("topicList")
    return topics if isinstance(topics, list) else []


def _read_dom_vacancy_ids(page: Page) -> tuple[set[str], bool]:
    """vacancy_id DOM-карточек; bool — подтверждён ли рендер карточек."""
    cards = page.locator(ns.NEGOTIATION_ITEM)
    try:
        cards.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
    except PlaywrightError:
        return set(), False
    ids: set[str] = set()
    try:
        for i in range(cards.count()):
            item = parse_response_card(cards.nth(i))
            if item is not None:
                ids.add(item.vacancy_id)
    except PlaywrightError:
        # Карточки есть (рендер подтверждён) — непрочитавшаяся часть не отменяет
        # чистоту чтения, но и не должна ронять проверку целиком.
        return ids, True
    return ids, True


def _has_next_page_confirmed(page: Page, page_num: int) -> bool:
    try:
        return _has_next_page(page, page_num)
    except ResponsesIndeterminate:
        # Неподтверждённый пагинатор — не причина indeterminate-вердикта:
        # свежий отклик был бы на странице 0 (сортировка по свежести).
        logger.warning("[VERIFY] пагинация не подтверждена — сканирую только прочитанное")
        return False
