"""Детекция тест-вопросов/анкет в форме отклика (#95, detect-only).

Владелец: #95. НЕ авто-ответ: если в форме отклика есть вопросы (hh.ru questionnaire),
возвращаем has_questions=True — pipeline/probe останавливаются БЕЗ submit и пишут
вакансию в журнал skipped (#87), чтобы повторный search не пересматривал её.

Источники селекторов:
  - data-qa task-body/task-question — подтверждено konard reference (production hh.ru).
  - heuristic (input[type=radio|checkbox], textarea вне cover-letter) — НЕ подтверждено
    на нашем аккаунте, запасная эвристика на случай если hh.ru поменяет data-qa.
Порядок: (1) data-qa task-body > 0 → True; (2) heuristic: любое radio/checkbox ИЛИ
textarea за вычетом известных cover-letter textareas → True; иначе False.

Чистая функция поверх page.locator().count() — без браузера тестируется на HTML-фикстуре.

ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ (round 3, НЕ подтверждено на живом hh.ru): heuristic-путь
(2) скоупится через ближайший <form>-предок APPLY_SUBMIT_BUTTON (см. _form_scope).
konard-референс подтверждает только наличие data-qa атрибутов, а НЕ то, что кнопка
submit обёрнута именно в семантический <form>-тег — этот факт нигде не проверен
живым дампом (форма отклика в CLAUDE.md сама помечена «НЕ подтверждено»). Если
допущение неверно (SPA без <form>, submit через onClick — обычная практика для
React), _form_scope() будет систематически возвращать None → detect_questions()
всегда вернёт indeterminate=True → pipeline будет fail'ить КАЖДЫЙ non-dry-run
apply ещё до fill_response_form, независимо от наличия вопросов в форме.
ОБЯЗАТЕЛЬНО перед первым боевым apply/bump: прогнать `probe` на реальной вакансии
и проверить лог на "[WARN indeterminate]" — если он появляется на форме БЕЗ
вопросов, admission `<form>`-скоупинга неверно и требует ревизии (см. #95).
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..browser import PAGE_STATE
from ..selector_groups import apply_form

# Heuristic-селекторы (НЕ data-qa, поэтому живут здесь, а не в selector_groups):
# любое radio/checkbox в форме отклика = вопрос (нормальная форма отклика их не имеет);
# любая textarea, не совпадающая с cover-letter селекторами = вопрос.
_RADIO = "input[type='radio']"
_CHECKBOX = "input[type='checkbox']"
_TEXTAREA = "textarea"

# Множество известных cover-letter textareas (форма отклика ИЛИ full-page вариант).
# Используется heuristic: textarea вне этого множества = ответ на вопрос.
_COVER_LETTER_TEXTAREAS = (
    apply_form.APPLY_COVER_LETTER_TEXTAREA,  # popup-форма
    apply_form.APPLY_COVER_LETTER_TEXTAREA_FORM,  # full-page форма (konard)
)

# #139: bounded-ожидание перед первым чтением DOM формы отклика (не сразу после
# навигации: navigate_to_response_form уже ждёт APPLY_SUBMIT_BUTTON, но
# task-body/radio/checkbox/textarea рендерятся отдельным JS-проходом и могут
# появиться позже). Короткий таймаут — как OPTIONAL_FIELD_TIMEOUT_MS в apply/steps.py:
# элемент либо появится быстро, либо детерминированно отсутствует.
_QUESTION_WAIT_TIMEOUT_MS = 1_500


@dataclass(frozen=True)
class QuestionDetection:
    """Результат детекции вопросов. has_questions=True → skip без submit (#95).

    indeterminate=True (round-2 fix): границы формы не удалось определить
    (нет <form>-предка у кнопки submit), heuristic-часть не выполнялась — тоже
    отправку блокируем (fail-closed), НО pipeline трактует это как обычный fail,
    а не как подтверждённый skip: без persistent-записи в skipped (#87), иначе
    один неопределившийся scope навсегда блокирует вакансию по недостоверной
    причине (тот же класс бага, что unscoped heuristic в round 1).
    """

    has_questions: bool
    reason: str = ""
    indeterminate: bool = False

    @property
    def page_state(self) -> str:
        """Общее состояние страницы без изменения старого bool-контракта."""
        return PAGE_STATE["indeterminate"] if self.indeterminate else PAGE_STATE["confirmed"]

    @classmethod
    def no(cls) -> QuestionDetection:
        return cls(False, "")

    @classmethod
    def yes(cls, reason: str) -> QuestionDetection:
        return cls(True, reason)

    @classmethod
    def indeterminate_scope(cls, reason: str) -> QuestionDetection:
        return cls(True, reason, indeterminate=True)


_SCOPE_NOT_FOUND_REASON = (
    "не удалось определить границы формы отклика (нет связанного <form> у submit)"
)
_RUNTIME_ERROR_REASON = (
    "ошибка при проверке анкеты формы отклика — отправка отменена (нестабильная страница)"
)


def _wait_present(locator: Locator, *, timeout_ms: int) -> bool | None:
    """Bounded-ожидание присутствия элемента в DOM (#139: замена голого count()).

    True — элемент появился (не факт видимости — используется state='attached',
    т.к. для heuristic/scope важно наличие в DOM, не видимость). False —
    детерминированно отсутствует (PlaywrightTimeoutError после полного
    таймаута — легитимный «элемента нет»). None — аномалия (strict-mode
    violation и подобные PlaywrightError, НЕ таймаут) — состояние не
    подтверждено, вызывающий обязан трактовать это fail-closed, но не как
    подтверждённое «вопрос есть» для persistent-skip.
    """
    try:
        locator.first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    except PlaywrightError:
        return None
    return True


def _form_scope(page: Page) -> Locator | None:
    """Ищет связанную с кнопкой submit форму — граница heuristic-поиска
    (#95 round-1 fix). Возвращает Locator при успехе, None если <form>-предок
    не найден — НЕТ fallback на весь page (round-2 fix): без надёжной границы
    heuristic не выполняется вовсе, чтобы посторонний page-level
    radio/checkbox/textarea не порождал ложный persistent skip.

    #139: раньше наличие проверялось голым ``count() > 0`` сразу после
    навигации — гонка рендера (форма ещё не отрисовалась) давала 0 и ложный
    None, хотя <form>-предок в итоге появлялся. Теперь ждём bounded таймаутом.

    cycle-review #139: этот wait_for подтверждает только, что <form>-предок
    существовал НА МОМЕНТ ожидания — возвращаемый ``scope`` сам по себе не несёт
    гарантию для дочерних локаторов (``scope.locator(...)`` ниже в
    detect_questions), каждый из них дожидается своего состояния независимо.
    """
    submit = page.locator(apply_form.APPLY_SUBMIT_BUTTON).first
    scope = page.locator(f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]")
    present = _wait_present(scope, timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
    if present:
        return scope

    # The response modal renders its footer button outside the form and links
    # it with the HTML ``form`` attribute (for example, RESPONSE_MODAL_FORM_ID).
    try:
        form_id = submit.get_attribute("form")
    except PlaywrightError:
        return None
    if not form_id:
        return None
    linked_scope = page.locator(f"form#{form_id}")
    return (
        linked_scope
        if _wait_present(linked_scope, timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
        else None
    )


def detect_questions(page: Page) -> QuestionDetection:
    """Чистая проверка формы отклика на наличие вопросов/анкеты (#95).

    Возвращает QuestionDetection. Вызывается ПОСЛЕ navigate_to_response_form и
    ДО fill_response_form (pipeline) / ДО dump (probe). has_questions=True означает
    «отправка отменена, вакансия требует ручного ответа».

    Порядок проверок (fail-closed — при сомнении склоняемся к «вопрос есть»):
      1. data-qa task-body (подтверждено konard): дожидаемся bounded-таймаутом,
         затем count() > 0 → yes.
      2. Heuristic (НЕ подтверждено), скоуплено внутрь <form>: radio/checkbox в
         любом количестве → yes; либо textarea, не входящая в известные
         cover-letter textareas → yes. Если границы формы не резолвятся —
         indeterminate (round-2 fix): блокируем отправку, но БЕЗ persistent skip.
    Никаких кликов, заполнений, навигаций — только локаторы (ожидание + count()).

    #139: раньше task-body/radio/checkbox/textarea читались голым count() сразу
    после навигации на форму — не успевшая отрисоваться анкета давала 0 вопросов
    → pipeline шёл в fill_response_form → submit с пропущенной анкетой.
    Отсутствие после bounded-ожидания ≠ отсутствие сразу: первое — «анкеты нет»,
    второе — «не подтвердили» (не различалось раньше, различается теперь).
    Любая иная (не-timeout) ошибка при ожидании — тоже не подтверждённое
    состояние, fail-closed → indeterminate, а не молчаливое «вопросов нет».
    """
    # (1) Подтверждённый data-qa путь — task-body специфичен, скоупинг не нужен.
    task_body = page.locator(apply_form.APPLY_QUESTION_BODY)
    task_body_present = _wait_present(task_body, timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
    if task_body_present is None:
        return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
    if task_body_present:
        return QuestionDetection.yes("вакансия требует заполнения анкеты (task-body)")

    # (2) Heuristic fallback, скоуплено внутрь формы отклика. Без формы-границы
    # heuristic не выполняется — indeterminate вместо unscoped-поиска по page.
    scope = _form_scope(page)
    if scope is None:
        return QuestionDetection.indeterminate_scope(_SCOPE_NOT_FOUND_REASON)

    radio_present = _wait_present(scope.locator(_RADIO), timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
    if radio_present is None:
        return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
    checkbox_present = (
        False
        if radio_present
        else _wait_present(scope.locator(_CHECKBOX), timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
    )
    if checkbox_present is None:
        return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
    if radio_present or checkbox_present:
        return QuestionDetection.yes("вакансия требует заполнения анкеты (radio/checkbox)")

    # textarea: все минус cover-letter, в пределах формы. Ждём появление хотя бы
    # одной textarea перед подсчётом (та же гонка рендера, что у radio/checkbox).
    #
    # cycle-review #139: раньше _wait_present() ждал на ОДНОРАЗОВОМ локаторе, а
    # решающий count() брался на свежесозданном (тот же селектор, но новый вызов
    # scope.locator(...)) — то есть count() снова читался БЕЗ собственного
    # ожидания, ровно та гонка, которую фикс должен был убрать. Опаснее всего
    # было для cover_letter_count: он вообще не ждался, только count(). Если
    # cover-letter textarea рендерится позже вопрос-textarea, cover_letter_count
    # читает 0, total_textareas — 1, и ЧИСТАЯ форма ложно детектится как анкета
    # → persistent skip (#87) навсегда хоронит нормальную вакансию — тот же
    # класс бага, что round-1/round-2 fix уже закрывали для form-scope.
    # Фикс: переиспользуем ОДИН дождавшийся локатор для total_textareas и ждём
    # (bounded) каждый cover-letter локатор отдельно перед его count().
    textarea_loc = scope.locator(_TEXTAREA)
    textarea_present = _wait_present(textarea_loc, timeout_ms=_QUESTION_WAIT_TIMEOUT_MS)
    if textarea_present is None:
        return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
    if textarea_present:
        total_textareas = textarea_loc.count()
        if total_textareas == 0:
            # TOCTOU: attached при wait_for, исчезла к count() — нестабильная
            # форма, а не «textarea нет» (та ветка уже отработала через False
            # выше). Fail-closed: не молчаливое no(), а indeterminate.
            return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
        cover_letter_count = 0
        for cover_letter_sel in _COVER_LETTER_TEXTAREAS:
            cover_letter_loc = scope.locator(cover_letter_sel)
            cover_letter_present = _wait_present(
                cover_letter_loc, timeout_ms=_QUESTION_WAIT_TIMEOUT_MS
            )
            if cover_letter_present is None:
                return QuestionDetection.indeterminate_scope(_RUNTIME_ERROR_REASON)
            if cover_letter_present:
                cover_letter_count += cover_letter_loc.count()
        if total_textareas > cover_letter_count:
            return QuestionDetection.yes(
                "вакансия требует заполнения анкеты (textarea вне cover-letter)"
            )

    return QuestionDetection.no()
