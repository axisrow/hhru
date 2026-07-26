"""Форма отклика на /applicant/vacancy_response — подтверждённые shared-селекторы.

Смягчение #3↔#7: здесь только shared-селекторы формы (resume-select, letter
toggle/textarea, submit), которые #3 и #7 не трогают. Селекторы статуса отклика
живут у своих владельцев:
  - «уже откликались» → apply/dedup.py (#3)
  - успешная отправка  → apply/success.py (#7)
Так они полностью изолированы друг от друга.
"""

from __future__ import annotations

APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"
