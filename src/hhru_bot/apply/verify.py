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

#212: атрибуция по резюме. Домены id у hh.ru несовместимы: конфиг адресует
резюме хэшем из URL, а SSR темы несут числовой ``resumeId``. Сравнение тех и
других как строк делало каждую тему «чужой» — ``found`` был недостижим в
продакшене с момента PR #209 (false negative #3, вакансия 135170581).
Правило здесь — ``_resume_attribution``: прямой match надёжен в любом
домене; «другое резюме» доказуемо только по перечню резюме аккаунта
(``account_resume_ids`` от ``copy_resume.resolve_numeric_resume_ids``);
без перечня разные строки — incomparable → fail-closed indeterminate, а не
молчаливый not_found. Важно (цикл 2, Codex): тема с ДРУГИМ собственным
резюме аккаунта (форма отклика могла приложить default-резюме, а не
конфиг-резюме) НЕ подтверждает текущий apply — она могла быть создана
предыдущим откликом, а не этим кликом. Подтверждать по ней нельзя: ложный
success перманентно подавил бы повторную попытку через дедупликацию.
Поэтому «другое собственное» → other_own → fail-closed indeterminate
(как incomparable), а не found.
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
from .verdict import (
    Completeness,
    PageRead,
    PageSource,
    Partial,
    ResumeAttribution,
    TopicRead,
    compose,
)

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
#: Инвариант «свежий отклик на странице 0» проверен живой пробой 2026-08-16
#: (#210): все 14 тем аккаунта строго по creationTime по убыванию, свежайшая —
#: на странице 0. Решение по напряжению 1 #210: документировать инвариант,
#: НЕ переводя неподтверждённый пагинатор на fail-closed (0 случаев
#: ResponsesIndeterminate в логах на ту дату — эвристика не стреляет).
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

#: Атрибуция темы резюме (#212): matched — тема подтверждает отклик; foreign —
#: доказуемо чужое резюме (continue скана); incomparable — сравнить нельзя
#: (fail-closed indeterminate у вызывающего кода); other_own — ДРУГОЕ собственное
#: резюме аккаунта, но не текущее (fail-closed indeterminate: тема могла быть
#: создана предыдущим откликом, а не этим кликом — Codex-ревью цикла 2).
_RESUME_MATCHED = ResumeAttribution.MATCHED
_RESUME_FOREIGN = ResumeAttribution.FOREIGN
_RESUME_INCOMPARABLE = ResumeAttribution.INCOMPARABLE
_RESUME_OTHER_OWN = ResumeAttribution.OTHER_OWN


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
    page: Page,
    vacancy_id: str | None,
    resume_id: str | None = None,
    account_resume_ids: set[str] | None = None,
) -> NegotiationsVerifyResult:
    """Проверяет, присутствует ли вакансия в /applicant/negotiations.

    «Подтверждённо прочитанный» список (хотя бы одна чистая попытка из
    NEGOTIATIONS_VERIFY_ATTEMPTS) без вакансии — это ``not_found``, а не
    «не знаем»: серверный topicList — тот же источник, из которого hh.ru
    рисует карточки. Ни одной чистой попытки — ``indeterminate``.

    ``resume_id``/#212 — числовой id резюме из маппинга
    ``resolve_numeric_resume_ids`` (или хэш конфига, если маппинг не
    получен); ``account_resume_ids`` — перечень числовых id всех резюме
    аккаунта для отличения «другое собственное резюме» от «чужого»
    (см. ``_resume_attribution``).
    """
    if not vacancy_id:
        return NegotiationsVerifyResult(INDETERMINATE, "vacancy_id карточки неизвестен")
    wanted = str(vacancy_id)
    clean_read = False
    confirmed_incomplete = False
    attribution_incomparable = False
    problem = "список откликов не прочитан"
    seen_vacancy_ids: set[str] = set()
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
            found_detail, clean, page_problem, incomplete, page_attribution_incomparable = (
                _scan_negotiations(page, wanted, resume_id, account_resume_ids, seen_vacancy_ids)
            )
            if found_detail is not None:
                logger.info("[VERIFY] отклик подтверждён: %s", found_detail)
                return NegotiationsVerifyResult(FOUND, found_detail)
            clean_read = clean_read or clean
            confirmed_incomplete = confirmed_incomplete or incomplete
            attribution_incomparable = attribution_incomparable or page_attribution_incomparable
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
    if attribution_incomparable:
        # #212: хотя бы одна попытка нашла тему целевой вакансии, но не смогла
        # атрибутировать резюме (несовместимые домены id без маппинга) —
        # absence не подтверждаем. Отдельный флаг (не только problem): чистое
        # чтение ДРУГОЙ попытки не должно замаскировать incomparable (иначе
        # ложный not_found — ровно тот false-negative, что #212 устраняет).
        return _indeterminate(
            page, wanted, problem or "тема вакансии найдена, но атрибуция резюме невозможна"
        )
    if clean_read:
        # #212: not_found не оставлял артефакта для аудита — «точно нет» без
        # следов того, что именно прочитано, нечем опровергать постфактум.
        # Печатаем прочитанные vacancy_id (все страницы всех попыток).
        logger.info(
            "[VERIFY] список прочитан, vacancy_id=%s отсутствует (прочитано: %s)",
            wanted,
            ", ".join(sorted(seen_vacancy_ids)) or "пусто",
        )
        return NegotiationsVerifyResult(NOT_FOUND, "список откликов прочитан, вакансии нет")
    return _indeterminate(page, wanted, problem)


def _indeterminate(page: Page, vacancy_id: str, detail: str) -> NegotiationsVerifyResult:
    # Дамп в стиле #195: неразобравшийся случай должен оставлять артефакты
    # для посмертной диагностики (селектор — первый подозреваемый, CLAUDE.md).
    _dump_navigation_diagnostics(page, "verify_indeterminate", vacancy_id)
    return NegotiationsVerifyResult(INDETERMINATE, detail)


def _scan_negotiations(
    page: Page,
    wanted: str,
    resume_id: str | None,
    account_resume_ids: set[str] | None,
    seen_vacancy_ids: set[str],
) -> tuple[str | None, bool, str | None, bool, bool]:
    """Сканирует страницу 0 (+ следующую при подтверждённой пагинации).

    Возвращает (detail найденной темы | None, было ли чистое чтение,
    описание проблемы чтения | None, подтверждена ли пагинация, но не дочитана
    страница, найдена ли тема с неатрибутируемым резюме). Чистое чтение —
    только когда НИ ОДНА страница не дала проблему: если какая-то страница не
    загрузилась, карточка не прочиталась или тему совпавшей вакансии не удалось
    атрибутировать по резюме (#212), отсутствие целевой вакансии не подтверждаем
    (она могла быть на непрочитанной странице) — иначе ложный not_found.
    confirmed_incomplete отдельно от clean: попытка, где пагинатор подтвердил
    продолжение, но страница не дочиталась, обязана перевесить чистые чтения
    ДРУГИХ попыток (иначе OR-агрегация в verify_response_in_negotiations
    замаскировала бы неполный скан, #207). attribution_incomparable — тема
    целевой вакансии найдена, но резюме не атрибутируется (несовместимые домены
    id без маппинга): тоже обязана перевесить чистые чтения других попыток
    (иначе ложный not_found, #212).
    """
    clean = False
    problem: str | None = None
    confirmed_incomplete = False
    attribution_incomparable = False
    reads: list[PageRead] = []
    for page_num in range(NEGOTIATIONS_VERIFY_MAX_PAGES):
        if page_num > 0:
            try:
                goto_hh(page, f"{NEGOTIATIONS_URL}?page={page_num}")
            except PlaywrightError as exc:
                # Пагинация была подтверждена на странице 0 (мы сюда дошли) —
                # страница 1 не дочитана: целевая вакансия могла быть на ней.
                problem = f"goto страницы {page_num} списка не прошёл ({exc})"
                confirmed_incomplete = True
                reads.append(PageRead(PageSource.SSR, (), Partial(problem)))
                break
        found_detail, page_clean, page_problem, page_attribution_incomparable = _scan_single_page(
            page, wanted, resume_id, account_resume_ids, seen_vacancy_ids
        )
        attribution_incomparable = attribution_incomparable or page_attribution_incomparable
        if page_attribution_incomparable:
            reads.append(
                PageRead(
                    PageSource.SSR,
                    (TopicRead(wanted, ResumeAttribution.INCOMPARABLE),),
                    Completeness.LAST_CONFIRMED,
                )
            )
        elif page_clean:
            reads.append(PageRead(PageSource.SSR, (), Completeness.LAST_CONFIRMED))
        else:
            reads.append(
                PageRead(
                    PageSource.SSR, (), Partial(page_problem or "страница прочитана не полностью")
                )
            )
        if page_problem:
            problem = page_problem
        if found_detail is not None:
            return found_detail, True, None, False, False
        clean = clean or page_clean
        if not _has_next_page_confirmed(page, page_num):
            break
        if page_num == NEGOTIATIONS_VERIFY_MAX_PAGES - 1:
            # Достигли потолка скана, но пагинатор подтверждает продолжение —
            # целевая вакансия могла быть на непросканированной странице 2+.
            # Fail-closed: indeterminate, а не ложный not_found (#207).
            problem = "достигнут потолок скана при подтверждённой пагинации"
            confirmed_incomplete = True
            reads.append(PageRead(PageSource.SSR, (), Partial(problem)))
            break
    # Keep the legacy tuple for the browser reader, but let the typed decision
    # table own the final absence/uncertainty policy (#213).
    composed = compose(reads, wanted)
    if composed == "not_found":
        clean = True
    elif composed == "indeterminate":
        clean = False
    return None, clean and not problem, problem, confirmed_incomplete, attribution_incomparable


def _scan_single_page(
    page: Page,
    wanted: str,
    resume_id: str | None,
    account_resume_ids: set[str] | None,
    seen_vacancy_ids: set[str],
) -> tuple[str | None, bool, str | None, bool]:
    try:
        html = page.content()
    except PlaywrightError as exc:
        return None, False, f"page.content() упал ({exc})"
    topics = _ssr_topic_list(html)
    if topics is not None:
        # SSR — серверная истина; DOM читает те же данные, fallback не нужен.
        seen_vacancy_ids.update(str(t.get("vacancyId")) for t in topics if t.get("vacancyId"))
        attribution_problem: str | None = None
        for topic in topics:
            if str(topic.get("vacancyId", "")) != wanted:
                continue
            attribution = _resume_attribution(topic, resume_id, account_resume_ids)
            if attribution == _RESUME_FOREIGN:
                # Отклик с доказуемо чужого резюме — не подтверждение apply:
                # продолжаем искать тему с нашим резюме.
                continue
            if attribution == _RESUME_INCOMPARABLE:
                # #212: тема этой вакансии есть, но сравнить резюме нечем
                # (несовместимые домены id без маппинга) — absence не
                # подтверждаем. Скан продолжается: прямой match по resume_id
                # ниже по списку всё ещё доказал бы found.
                attribution_problem = (
                    f"тема vacancy_id={wanted} найдена, но атрибуция резюме невозможна: "
                    f"resumeId темы={topic.get('resumeId')} против резюме конфига="
                    f"{resume_id} (несовместимые домены id, #212)"
                )
                continue
            if attribution == _RESUME_OTHER_OWN:
                # Цикл 2 (Codex): тема с ДРУГИМ собственным резюме аккаунта
                # могла быть создана ПРЕДЫДУЩИМ откликом, а не этим кликом —
                # подтверждать текущий apply по ней нельзя (иначе ложный
                # success и перманентная дедупликация подавит повторную
                # попытку). Fail-closed indeterminate, как incomparable; скан
                # продолжается: exact match ниже по списку всё ещё доказал бы
                # found.
                attribution_problem = (
                    f"тема vacancy_id={wanted} найдена, но с ДРУГИМ собственным "
                    f"резюме аккаунта (resumeId темы={topic.get('resumeId')} против "
                    f"резюме конфига={resume_id}) — не подтверждаем: тема могла "
                    f"быть создана предыдущим откликом"
                )
                continue
            return _describe_topic(topic), True, None, False
        return (
            None,
            attribution_problem is None,
            attribution_problem,
            attribution_problem is not None,
        )
    dom_ids, cards_seen = _read_dom_vacancy_ids(page)
    seen_vacancy_ids.update(dom_ids)
    if wanted in dom_ids:
        if resume_id is not None:
            # DOM-карточка не несёт resumeId — не можем атрибутировать отклик
            # к текущему резюме: он мог уйти с ДРУГОГО. Fail-closed
            # indeterminate, а не ложный success (как SSR-путь, #207).
            # attribution_incomparable=True: находка вакансии без атрибуции
            # обязана перевесить чистое чтение ДРУГОЙ попытки (иначе ложный
            # not_found — тот же false negative, что #212 устраняет).
            return None, False, "DOM-карточка без атрибуции резюме — исход неопределён", True
        return "DOM-карточка списка (SSR-состояние недоступно)", True, None, False
    if cards_seen:
        return None, True, None, False
    return None, False, "список не отрендерился (нет ни SSR-состояния, ни карточек)", False


def _resume_attribution(
    topic: dict[str, Any], resume_id: str | None, account_resume_ids: set[str] | None
) -> str:
    """Атрибуция темы резюме: matched | foreign | incomparable (#212).

    Домены id несовместимы: конфиг — хэш из URL резюме, SSR темы — числовой
    ``resumeId``. Прямое сравнение тех и других как строк — баг #212: каждая
    тема «чужая», found недостижим. Правила:

    * равные строки — matched (надёжно в любом домене);
    * ``account_resume_ids`` задан (маппинг из /applicant/resumes получен):
      id темы в перечне, но НЕ равный конфиг-резюме — other_own (ДРУГОЕ
      собственное резюме аккаунта). Это НЕ подтверждение текущего apply:
      тема могла быть создана предыдущим откликом с этого резюме, а не этим
      кликом (Codex-ревью цикла 2) — fail-closed indeterminate, а не found.
      (До цикла 2 «другое собственное» считалось matched по посылке #212
      «форма подписывает тему выбранным резюме»; но это не отличает свежую
      тему от старой, и ложный success перманентно подавил бы повторную
      попытку через дедупликацию.) Вне перечня — foreign (аномалия данных);
    * маппинга нет и строки разные — incomparable: доказать нельзя ни matched,
      ни foreign → вызывающий код обязан ответить indeterminate (fail-closed),
      а не молчаливым not_found (повторный отклик) или success (ложный).
    * resume_id не задан или у темы нет поля — matched: атрибутировать нечем,
      ронять подтверждение нельзя (resumeId присутствует у всех тем в живой
      практике: 14/14 #210, 7/7 #204 — напряжение 2 #210 закрыто фактом).
    """
    if resume_id is None:
        return _RESUME_MATCHED
    topic_resume = topic.get("resumeId")
    if topic_resume is None:
        return _RESUME_MATCHED
    if str(topic_resume) == str(resume_id):
        return _RESUME_MATCHED
    if account_resume_ids is not None:
        if str(topic_resume) in account_resume_ids:
            return _RESUME_OTHER_OWN
        return _RESUME_FOREIGN
    return _RESUME_INCOMPARABLE


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
    # parse_initial_state возвращает любой валидный JSON, не только объект:
    # null/массив/строка (schema-drift) — .get() на них бросил бы AttributeError
    # уже вне try. Нормализуем как «состояние недоступно» (fail-closed).
    if not isinstance(state, dict):
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
        # свежий отклик был бы на странице 0. Инвариант сортировки по
        # свежести проверен живой пробой 2026-08-16 (#210): 14/14 тем строго
        # по creationTime по убыванию; 0 случаев ResponsesIndeterminate в
        # логах на ту дату — оставляем as-is (документированное решение по
        # напряжению 1 #210, не fail-closed).
        logger.warning("[VERIFY] пагинация не подтверждена — сканирую только прочитанное")
        return False
