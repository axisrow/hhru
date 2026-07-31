"""Форма отклика на /applicant/vacancy_response — подтверждённые shared-селекторы.

Смягчение #3↔#7: здесь только shared-селекторы формы (resume-select, letter
toggle/textarea, submit), которые #3 и #7 не трогают. Селектор успешной отправки
живёт у владельца — apply/success.py (#7). «Уже откликались» (#3) селектора не
имеет вовсе: дедупликация идёт через history.has_applied() в filter_candidates(),
см. apply/dedup.py.
"""

from __future__ import annotations

APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"

# --- #95: детекция тест-вопросов/анкет в форме отклика (detect-only, NO auto-answer) ---
# Подтверждено konard reference (hh-selectors.mjs / qa.mjs, production hh.ru automation).
# task-body — контейнер вопроса; task-question — текст вопроса внутри него. На нашем
# аккаунте НЕ сверялись живым дампом, но konard использует их в боевом коде.
APPLY_QUESTION_BODY = "[data-qa='task-body']"  # подтверждено (konard)
APPLY_QUESTION_TEXT = "[data-qa='task-question']"  # подтверждено (konard), внутри task-body

# Второй (full-page) вариант textarea сопроводительного письма — нужен heuristic-фильтру,
# чтобы не принять cover-letter textarea за ответ на вопрос. konard: coverLetterTextareaForm.
APPLY_COVER_LETTER_TEXTAREA_FORM = "textarea[data-qa='vacancy-response-form-letter-input']"

# Heuristic-селекторы (НЕ data-qa, поэтому живут в apply/questions.py, а не в selector_groups):
# input[type='radio'], input[type='checkbox'], голый textarea — они используются
# detect_questions для fallback-эвристики, когда task-body переименован hh.ru.
