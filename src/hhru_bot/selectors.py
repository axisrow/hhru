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
    VACANCY_CARD_COMPANY,
    VACANCY_CARD_COMPENSATION,
    VACANCY_CARD_RESPONSE_BUTTON,
    VACANCY_CARD_RESPONSE_STATUS,
    VACANCY_CARD_TITLE_LINK,
    VACANCY_SEARCH_EMPTY,
)
from .selector_groups.vacancy_page import (
    VACANCY_APPLY_BUTTON,
    VACANCY_COMPANY_NAME,
    VACANCY_TITLE,
)

__all__ = [
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
    "VACANCY_CARD",
    "VACANCY_CARD_COMPANY",
    "VACANCY_CARD_COMPENSATION",
    "VACANCY_CARD_RESPONSE_BUTTON",
    "VACANCY_CARD_RESPONSE_STATUS",
    "VACANCY_CARD_TITLE_LINK",
    "VACANCY_SEARCH_EMPTY",
    "VACANCY_COMPANY_NAME",
    "VACANCY_TITLE",
]
