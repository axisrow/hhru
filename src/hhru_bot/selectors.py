"""Shim для обратной совместимости: переэкспортирует селекторы из selector_groups/.

Канонический источник селекторов теперь — пакет selector_groups/ (по страницам).
Этот модуль сохраняет старый плоский API (`from . import selectors as sel;
sel.VACANCY_CARD`), чтобы не править потребителей одновременно с разделением.

Новый код должен импортировать из конкретной группы:
    from .selector_groups import search_page, apply_form, ...

Статус проверки селекторов (подтверждено curl-дампом / НЕ подтверждено) описан
в модуле каждой группы.
"""

from __future__ import annotations

from .selector_groups.apply_form import (
    APPLY_COVER_LETTER_TEXTAREA,
    APPLY_COVER_LETTER_TOGGLE,
    APPLY_RESUME_SELECT,
    APPLY_SUBMIT_BUTTON,
)
from .selector_groups.resume_page import RESUME_BUMP_BUTTON, RESUME_BUMP_DISABLED_HINT
from .selector_groups.search_page import (
    COMPANY_RATING_REVIEWS_COUNT,
    COMPANY_RATING_VALUE,
    PAGINATION_BLOCK,
    PAGINATION_NEXT,
    PAGINATION_PAGE,
    TRUSTED_EMPLOYER_LINK,
    VACANCY_CARD,
    VACANCY_CARD_ADDRESS,
    VACANCY_CARD_COMPANY,
    VACANCY_CARD_COMPENSATION,
    VACANCY_CARD_EXPERIENCE,
    VACANCY_CARD_ACTIVITY,
    VACANCY_CARD_HH_RATING,
    VACANCY_CARD_HRBRAND_WINNER,
    VACANCY_CARD_METRO_STATION,
    VACANCY_CARD_NO_RESUME,
    VACANCY_CARD_PUBLICATION_TIME,
    VACANCY_CARD_REMOTE_LABEL,
    VACANCY_CARD_RESPONSE_BUTTON,
    VACANCY_CARD_RESPONSE_STATUS,
    VACANCY_CARD_SIDE_JOB,
    VACANCY_CARD_SNIPPET_REQUIREMENT,
    VACANCY_CARD_SNIPPET_RESPONSIBILITY,
    VACANCY_CARD_TITLE_LINK,
    VACANCY_SEARCH_EMPTY,
)
from .selector_groups.vacancy_page import (
    VACANCY_ALREADY_RESPONDED_AGAIN,
    VACANCY_ALREADY_RESPONDED_CHAT,
    VACANCY_APPLY_BUTTON,
    VACANCY_COMPANY_NAME,
    VACANCY_TITLE,
)

# Подтверждено headless-рендером https://hh.ru/account/login от 2026-08-13
# (после первого перехода на форму учётных данных, chromium, ru-RU).
LOGIN_CODE_REQUEST_BUTTON = "[data-qa='submit-button']"
LOGIN_EMAIL_TYPE = "input[data-qa='credential-type-email']"
LOGIN_EMAIL_INPUT = "[data-qa='applicant-login-input-email']"
LOGIN_PHONE_INPUT = "[data-qa='magritte-phone-input-national-number-input']"
# Подтверждено live DOM после запроса кода (2026-08-19); ввод запускает авто-submit.
LOGIN_CODE_INPUT = "[data-qa='magritte-pincode-input-field']"

__all__ = [
    "LOGIN_CODE_REQUEST_BUTTON",
    "LOGIN_EMAIL_TYPE",
    "LOGIN_EMAIL_INPUT",
    "LOGIN_PHONE_INPUT",
    "LOGIN_CODE_INPUT",
    "APPLY_COVER_LETTER_TEXTAREA",
    "APPLY_COVER_LETTER_TOGGLE",
    "APPLY_RESUME_SELECT",
    "APPLY_SUBMIT_BUTTON",
    "COMPANY_RATING_REVIEWS_COUNT",
    "COMPANY_RATING_VALUE",
    "PAGINATION_BLOCK",
    "PAGINATION_NEXT",
    "PAGINATION_PAGE",
    "RESUME_BUMP_BUTTON",
    "RESUME_BUMP_DISABLED_HINT",
    "TRUSTED_EMPLOYER_LINK",
    "VACANCY_APPLY_BUTTON",
    "VACANCY_ALREADY_RESPONDED_AGAIN",
    "VACANCY_ALREADY_RESPONDED_CHAT",
    "VACANCY_CARD",
    "VACANCY_CARD_ADDRESS",
    "VACANCY_CARD_COMPANY",
    "VACANCY_CARD_COMPENSATION",
    "VACANCY_CARD_EXPERIENCE",
    "VACANCY_CARD_ACTIVITY",
    "VACANCY_CARD_HH_RATING",
    "VACANCY_CARD_HRBRAND_WINNER",
    "VACANCY_CARD_METRO_STATION",
    "VACANCY_CARD_NO_RESUME",
    "VACANCY_CARD_PUBLICATION_TIME",
    "VACANCY_CARD_REMOTE_LABEL",
    "VACANCY_CARD_RESPONSE_BUTTON",
    "VACANCY_CARD_RESPONSE_STATUS",
    "VACANCY_CARD_SIDE_JOB",
    "VACANCY_CARD_SNIPPET_REQUIREMENT",
    "VACANCY_CARD_SNIPPET_RESPONSIBILITY",
    "VACANCY_CARD_TITLE_LINK",
    "VACANCY_SEARCH_EMPTY",
    "VACANCY_COMPANY_NAME",
    "VACANCY_TITLE",
]
