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
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Locator, Page

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

    @classmethod
    def no(cls) -> QuestionDetection:
        return cls(False, "")

    @classmethod
    def yes(cls, reason: str) -> QuestionDetection:
        return cls(True, reason)

    @classmethod
    def indeterminate_scope(cls, reason: str) -> QuestionDetection:
        return cls(True, reason, indeterminate=True)


_SCOPE_NOT_FOUND_REASON = "не удалось определить границы формы отклика (нет <form>-предка у submit)"


def _form_scope(page: Page) -> Locator | None:
    """Ищет ближайший предок-<form> кнопки submit — граница heuristic-поиска
    (#95 round-1 fix). Возвращает Locator при успехе, None если <form>-предок
    не найден — НЕТ fallback на весь page (round-2 fix): без надёжной границы
    heuristic не выполняется вовсе, чтобы посторонний page-level
    radio/checkbox/textarea не порождал ложный persistent skip.
    """
    scope = page.locator(f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]")
    return scope if scope.count() > 0 else None


def detect_questions(page: Page) -> QuestionDetection:
    """Чистая проверка формы отклика на наличие вопросов/анкеты (#95).

    Возвращает QuestionDetection. Вызывается ПОСЛЕ navigate_to_response_form и
    ДО fill_response_form (pipeline) / ДО dump (probe). has_questions=True означает
    «отправка отменена, вакансия требует ручного ответа».

    Порядок проверок (fail-closed — при сомнении склоняемся к «вопрос есть»):
      1. data-qa task-body (подтверждено konard): count() > 0 → yes.
      2. Heuristic (НЕ подтверждено), скоуплено внутрь <form>: radio/checkbox в
         любом количестве → yes; либо textarea, не входящая в известные
         cover-letter textareas → yes. Если границы формы не резолвятся —
         indeterminate (round-2 fix): блокируем отправку, но БЕЗ persistent skip.
    Никаких кликов, заполнений, навигаций — только count() поверх локаторов.
    """
    # (1) Подтверждённый data-qa путь — task-body специфичен, скоупинг не нужен.
    if page.locator(apply_form.APPLY_QUESTION_BODY).count() > 0:
        return QuestionDetection.yes("вакансия требует заполнения анкеты (task-body)")

    # (2) Heuristic fallback, скоуплено внутрь формы отклика. Без формы-границы
    # heuristic не выполняется — indeterminate вместо unscoped-поиска по page.
    scope = _form_scope(page)
    if scope is None:
        return QuestionDetection.indeterminate_scope(_SCOPE_NOT_FOUND_REASON)

    if scope.locator(_RADIO).count() > 0 or scope.locator(_CHECKBOX).count() > 0:
        return QuestionDetection.yes("вакансия требует заполнения анкеты (radio/checkbox)")

    # textarea: все минус cover-letter, в пределах формы. Если осталась хоть
    # одна — это вопрос-ответ.
    total_textareas = scope.locator(_TEXTAREA).count()
    cover_letter_count = sum(scope.locator(sel).count() for sel in _COVER_LETTER_TEXTAREAS)
    if total_textareas > cover_letter_count:
        return QuestionDetection.yes(
            "вакансия требует заполнения анкеты (textarea вне cover-letter)"
        )

    return QuestionDetection.no()
