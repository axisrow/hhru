"""Форма отклика на /applicant/vacancy_response — НЕ подтверждено (требует логина).

Смягчение #3↔#7: здесь только подтверждённые shared-селекторы формы
(resume-select, letter toggle/textarea, submit). Селекторы статуса отклика
живут во владельцах: «уже откликались» → apply/dedup.py (#3),
успешная отправка → apply/success.py (#7). До декомпозиции apply (паттерн 4)
они временно переэкспортируются через selectors.py shim.
"""

from __future__ import annotations

APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"

# Временное расположение — переедут во владельцев в паттерне 4 (apply/ пакет):
APPLY_SUCCESS_MARKER = "[data-qa='vacancy-response-sent-message']"
APPLY_ALREADY_RESPONDED_MARKER = (
    "[data-qa='vacancy-serp__vacancy_response_status']"  # НЕ подтверждено
)
