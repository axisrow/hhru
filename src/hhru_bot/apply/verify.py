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
#: странице 0 — глубокий скан не нужен. Достижение потолка при подтверждённой
#: пагинации — indeterminate, а не not_found (см. _scan_negotiations).
NEGOTIATIONS_VERIFY_MAX_PAGES = 2
#: Окно стабилизации DOM-списка в fallback-пути: карточки могут догружаться
#: (отложенный/виртуализированный рендер), и чтение count до стабилизации
#: дало бы ложный not_found. Ждём и перечитываем count (см. _dom_list_stable).
DOM_STABILITY_MS = 1_000

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
    confirmed_incomplete = False
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
            found_detail, clean, page_problem, incomplete = _scan_negotiations(
                page, wanted, resume_id
            )
            if found_detail is not None:
                logger.info("[VERIFY] отклик подтверждён: %s", found_detail)
                return NegotiationsVerifyResult(FOUND, found_detail)
            clean_read = clean_read or clean
            confirmed_incomplete = confirmed_incomplete or incomplete
            if page_problem:
                problem = page_problem
        if attempt < NEGOTIATIONS_VERIFY_ATTEMPTS:
            page.wait_for_timeout(NEGOTIATIONS_VERIFY_POLL_INTERVAL_MS)
    if confirmed_incomplete:
        # #207: хотя бы одна попытка подтвердила пагинацию, но не дочитала
        # страницу — целевая вакансия могла быть на непрочитанной странице.
        # Fail-closed: indeterminate, а не ложный not_found. Отдельный флаг
        # (не только clean): confirmed-incomplete из одной попытки перевешивает
        # чистое чтение другой (иначе OR-агрегация clean замаскировала бы
        # неполный скан, #207).
        return _indeterminate(
            page, wanted, problem or "подтверждённая пагинация, но не все страницы прочитаны"
        )
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
) -> tuple[str | None, bool, str | None, bool]:
    """Сканирует страницу 0 (+ следующую при подтверждённой пагинации).

    Возвращает (detail найденной темы | None, было ли чистое чтение,
    описание проблемы чтения | None, подтверждена ли пагинация, но не дочитана
    страница). Чистое чтение — только когда НИ ОДНА страница не дала проблему:
    если какая-то страница не загрузилась или карточка не прочиталась, отсутствие
    целевой вакансии не подтверждаем (она могла быть на непрочитанной странице) —
    иначе ложный not_found. confirmed_incomplete отдельно от clean: попытка, где
    пагинатор подтвердил продолжение, но страница не дочиталась, обязана
    перевесить чистые чтения ДРУГИХ попыток (иначе OR-агрегация в
    verify_response_in_negotiations замаскировала бы неполный скан, #207).
    """
    clean = False
    problem: str | None = None
    confirmed_incomplete = False
    for page_num in range(NEGOTIATIONS_VERIFY_MAX_PAGES):
        if page_num > 0:
            try:
                goto_hh(page, f"{NEGOTIATIONS_URL}?page={page_num}")
            except PlaywrightError as exc:
                # Пагинация была подтверждена на странице 0 (мы сюда дошли) —
                # страница 1 не дочитана: целевая вакансия могла быть на ней.
                problem = f"goto страницы {page_num} списка не прошёл ({exc})"
                confirmed_incomplete = True
                break
        found_detail, page_clean, page_problem = _scan_single_page(page, wanted, resume_id)
        if page_problem:
            problem = page_problem
        if found_detail is not None:
            return found_detail, True, None, False
        clean = clean or page_clean
        if not _has_next_page_confirmed(page, page_num):
            break
        if page_num == NEGOTIATIONS_VERIFY_MAX_PAGES - 1:
            # Достигли потолка скана, но пагинатор подтверждает продолжение —
            # целевая вакансия могла быть на непросканированной странице 2+.
            # Fail-closed: indeterminate, а не ложный not_found (#207).
            problem = "достигнут потолок скана при подтверждённой пагинации"
            confirmed_incomplete = True
            break
    return None, clean and not problem, problem, confirmed_incomplete


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
                if _is_foreign_resume(topic, resume_id):
                    # Отклик с ДРУГОГО резюме на ту же вакансию — не
                    # подтверждение apply с текущего: продолжаем искать тему
                    # с совпадающим resumeId (иначе ложный success, #207).
                    continue
                return _describe_topic(topic), True, None
        return None, True, None
    dom_ids, cards_seen = _read_dom_vacancy_ids(page)
    if wanted in dom_ids:
        if resume_id is not None:
            # DOM-карточка не несёт resumeId — не можем атрибутировать отклик
            # к текущему резюме: он мог уйти с ДРУГОГО. Fail-closed
            # indeterminate, а не ложный success (как SSR-путь, #207).
            return None, False, "DOM-карточка без атрибуции резюме — исход неопределён"
        return "DOM-карточка списка (SSR-состояние недоступно)", True, None
    if cards_seen:
        return None, True, None
    return None, False, "список не отрендерился (нет ни SSR-состояния, ни карточек)"


def _is_foreign_resume(topic: dict[str, Any], resume_id: str | None) -> bool:
    """Отклик с другого резюме — не подтверждение apply с текущего.

    resume_id не задан — атрибутировать нечем, считаем совпадением (resumeId
    присутствует у всех тем в практике, #200). Поле отсутствует у темы — тоже
    не можем отличить, не роняем подтверждение (fail-open, как topic_refs).
    """
    if resume_id is None:
        return False
    topic_resume = topic.get("resumeId")
    if topic_resume is None:
        return False
    return str(topic_resume) != str(resume_id)


def _describe_topic(topic: dict[str, Any]) -> str:
    detail = f"topic={topic.get('id')}" if topic.get("id") is not None else "topic=?"
    topic_resume = topic.get("resumeId")
    if topic_resume is not None:
        detail += f", resumeId={topic_resume}"
    return detail


def _ssr_topic_list(html: str) -> list[dict[str, Any]] | None:
    """topicList из SSR-состояния; None — состояние недоступно (не «пусто»).

    Сканирует сырые темы, а не topic_refs(): для проверки достаточно
    vacancyId, и политика дропа записей без id/chatId (для маппинга чатов)
    не должна превращать существующий отклик в «не найден».

    None возвращается и когда секция applicantNegotiations отсутствует или
    topicList не список: это «не отрендерилось», а не «пустой список» — иначе
    ложный not_found (false negative, который #207 и предотвращает).
    """
    try:
        state = parse_initial_state(html)
    except (ValueError, AttributeError):
        return None
    neg = state.get("applicantNegotiations")
    if not isinstance(neg, dict):
        return None
    topics = neg.get("topicList")
    return topics if isinstance(topics, list) else None


def _read_dom_vacancy_ids(page: Page) -> tuple[set[str], bool]:
    """vacancy_id DOM-карточек; bool — подтверждён ли рендер карточек.

    bool=True только при ПОЛНОМ чтении всех карточек. Непрочитавшаяся карточка
    (parse_response_card → None) или PlaywrightError посреди итерации — скан
    неполный: целевая вакансия могла быть в непрочитанной карточке, поэтому
    отсутствие не подтверждаем (иначе ложный not_found, #207). Список также
    должен быть стабилен после короткой паузы (см. _dom_list_stable): догрузка
    или подмена карточек в процессе чтения — тот же класс неполноты.
    """
    cards = page.locator(ns.NEGOTIATION_ITEM)
    try:
        cards.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
    except PlaywrightError:
        return set(), False
    initial = _read_dom_ids(page)
    if initial is None:
        return set(), False
    if not _dom_list_stable(page, initial):
        return initial, False
    return initial, True


def _read_dom_ids(page: Page) -> set[str] | None:
    """Текущий набор vacancy_id DOM-карточек; None — скан неполный.

    None при непрочитавшейся карточке (parse_response_card → None) или
    PlaywrightError посреди итерации: целевая вакансия могла быть в
    непрочитанной карточке, поэтому отсутствие не подтверждаем (#207).
    """
    cards = page.locator(ns.NEGOTIATION_ITEM)
    try:
        count = cards.count()
        ids: set[str] = set()
        for i in range(count):
            item = parse_response_card(cards.nth(i))
            if item is None:
                return None
            ids.add(item.vacancy_id)
        return ids
    except PlaywrightError:
        return None


def _dom_list_stable(page: Page, initial_ids: set[str]) -> bool:
    """True, если набор карточек стабилен после короткой паузы (список догрузился).

    DOM-fallback не имеет подтверждённого empty-state-сигнала, поэтому
    завершённость списка проверяем эвристикой: карточки могут догружаться
    (отложенный/виртуализированный рендер), и чтение текущего набора до
    стабилизации дало бы ложный not_found (#207). Сравниваем НАБОР vacancy_id
    свежим locator'ом, а не только count: подмена/переупорядочивание карточек
    при том же count (виртуализация) тоже означает нестабильность — иначе
    прочитанный набор мог быть устаревшим.
    """
    try:
        page.wait_for_timeout(DOM_STABILITY_MS)
        fresh = _read_dom_ids(page)
        return fresh is not None and fresh == initial_ids
    except PlaywrightError:
        return False


def _has_next_page_confirmed(page: Page, page_num: int) -> bool:
    try:
        return _has_next_page(page, page_num)
    except ResponsesIndeterminate:
        # Неподтверждённый пагинатор — не причина indeterminate-вердикта:
        # свежий отклик был бы на странице 0 (сортировка по свежести).
        logger.warning("[VERIFY] пагинация не подтверждена — сканирую только прочитанное")
        return False
