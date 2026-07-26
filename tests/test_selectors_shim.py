"""Characterization-тест: selectors.py shim переэкспортирует все прежние имена.

После разделения на selector_groups/ плоский API `sel.<NAME>` должен остаться
полностью идентичным — иначе потребители (search/apply/bump/auth) сломаются.
"""

from __future__ import annotations

from hhru_bot import selectors as sel
from hhru_bot.selector_groups import apply_form, login, resume_page, search_page, vacancy_page


def test_shim_reexports_all_old_names():
    expected = {
        # search_page
        "VACANCY_CARD",
        "VACANCY_CARD_TITLE_LINK",
        "VACANCY_CARD_COMPANY",
        "VACANCY_CARD_RESPONSE_BUTTON",
        "PAGINATION_NEXT",
        "VACANCY_CARD_RESPONSE_STATUS",
        # vacancy_page
        "VACANCY_APPLY_BUTTON",
        "VACANCY_TITLE",
        "VACANCY_COMPANY_NAME",
        # apply_form (shared-селекторы формы)
        "APPLY_RESUME_SELECT",
        "APPLY_COVER_LETTER_TOGGLE",
        "APPLY_COVER_LETTER_TEXTAREA",
        "APPLY_SUBMIT_BUTTON",
        # resume_page
        "RESUME_BUMP_BUTTON",
        "RESUME_BUMP_DISABLED_HINT",
        # login
        "LOGIN_URL_MARKER",
    }
    for name in expected:
        assert hasattr(sel, name), f"selectors.shim потерял имя {name}"


def test_status_markers_moved_to_owners():
    # Смягчение #3↔#7: маркеры статуса отклика НЕ в shim/apply_form —
    # они живут у владельцев (apply/dedup.py, apply/success.py).
    assert not hasattr(sel, "APPLY_SUCCESS_MARKER")
    assert not hasattr(sel, "APPLY_ALREADY_RESPONDED_MARKER")
    from hhru_bot.apply import dedup, success

    assert dedup.APPLY_ALREADY_RESPONDED_MARKER
    assert success.APPLY_SUCCESS_MARKER


def test_shim_values_match_groups():
    assert sel.VACANCY_CARD == search_page.VACANCY_CARD
    assert sel.VACANCY_APPLY_BUTTON == vacancy_page.VACANCY_APPLY_BUTTON
    assert sel.APPLY_SUBMIT_BUTTON == apply_form.APPLY_SUBMIT_BUTTON
    assert sel.RESUME_BUMP_BUTTON == resume_page.RESUME_BUMP_BUTTON
    assert sel.LOGIN_URL_MARKER == login.LOGIN_URL_MARKER
