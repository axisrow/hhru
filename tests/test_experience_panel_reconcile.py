"""#956: panel checkboxes mount asynchronously after "Развернуть".

Live failure 2026-09-03 (adding a second experience row on a draft):
right after the expand step, ``scope.get_by_role(...)`` returned count=0
for the LAST titles in the list — the expanded rows re-render
asynchronously and ``count()`` is instantaneous. Reconciliation must wait
(bounded) for a checkbox that is not immediately resolvable instead of
failing the row.
"""

from __future__ import annotations

import pytest

from hhru_bot.experience import _reconcile_experience_resume_panel

pytestmark = pytest.mark.unit


class _AsyncCheckbox:
    """count() is 0 until the first wait_for() call simulates the re-render."""

    def __init__(self):
        self._count = 0
        self._checked = True

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def wait_for(self, *, timeout=None, state=None):  # noqa: ARG002
        self._count = 1

    def is_checked(self):
        return self._checked

    def click(self, *, timeout=None, **_kwargs):
        self._checked = not self._checked


class _AsyncScopeLocator:
    def __init__(self, checkboxes, count=1):
        self._checkboxes = checkboxes
        self._count = count

    def count(self):
        return self._count

    def get_by_role(self, role, *, name, exact=False):
        assert role == "checkbox"
        assert exact is True
        return self._checkboxes.get(name) or _MissingCheckbox()


class _MissingCheckbox:
    """A row that never renders: count 0, waits time out (Playwright-like)."""

    def count(self):
        return 0

    @property
    def first(self):
        return self

    def wait_for(self, *, timeout=None, state=None):  # noqa: ARG002
        from playwright.sync_api import Error as _PWError

        raise _PWError("waiting for locator timed out")


class _AsyncPanelPage:
    """Serves the panel scope (count=1, role dispatch) and a collapsed
    expand button (count=0, so the expand branch is skipped)."""

    def __init__(self, checkboxes):
        self._checkboxes = checkboxes
        self._scope = _AsyncScopeLocator(checkboxes, count=1)

    def locator(self, selector):
        if selector == "panel-scope":
            return self._scope
        return _AsyncScopeLocator({}, count=0)

    def count(self):
        return 1

    def get_by_role(self, role, *, name, exact=False):
        assert role == "checkbox"
        assert exact is True
        return self._checkboxes[name]


def test_reconcile_waits_for_async_mounted_checkboxes(monkeypatch):
    checkboxes = {
        "Дворник": _AsyncCheckbox(),
        "Хирург": _AsyncCheckbox(),
    }
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_RESUME_PANEL_SCOPE", "panel-scope")
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_RESUME_PANEL_EXPAND", "panel-expand")
    page = _AsyncPanelPage(checkboxes)

    _reconcile_experience_resume_panel(
        page,
        target_title="Дворник",
        other_titles=["Хирург"],
        is_new_row=True,
    )

    # Target stays checked, the other resume's default-checked box is unticked.
    assert checkboxes["Дворник"].is_checked() is True
    assert checkboxes["Хирург"].is_checked() is False


def _page_with_expanded_panel(monkeypatch, checkboxes):
    class _ExpandablePanelPage(_AsyncPanelPage):
        def __init__(self, checkboxes):
            super().__init__(checkboxes)
            self.expand_clicks = 0

        def locator(self, selector):
            if selector == "panel-scope":
                return self._scope
            if selector == "panel-expand":
                return _ExpandLocator(self)
            return _AsyncScopeLocator({}, count=0)

    class _ExpandLocator:
        def __init__(self, page):
            self._page = page

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def wait_for(self, *, timeout=None, state=None):
            if state == "hidden" and self._page.expand_clicks > 0:
                return None
            if state == "visible":
                return None
            raise AssertionError(f"unexpected wait_for(state={state})")

        def click(self, *, timeout=None, **_kwargs):
            self._page.expand_clicks += 1

    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_RESUME_PANEL_SCOPE", "panel-scope")
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_RESUME_PANEL_EXPAND", "panel-expand")
    monkeypatch.setattr("hhru_bot.experience._wait_for_react_hydration", lambda *a, **k: None)
    return _ExpandablePanelPage(checkboxes)


def test_missing_other_checkbox_is_skipped_after_confirmed_expand(monkeypatch):
    """#956 live: hh.ru's panel can omit an account resume entirely (the
    fresh 'Хирург' draft had no row at all) while /applicant/resumes still
    lists it. After a confirmed expand, a missing OTHER checkbox is a safe
    skip (absent from DOM => cannot be pre-checked), not a failure."""
    checkboxes = {
        "Дворник": _AsyncCheckbox(),
        # 'Хирург' has NO checkbox in the panel at all.
    }
    page = _page_with_expanded_panel(monkeypatch, checkboxes)

    _reconcile_experience_resume_panel(
        page,
        target_title="Дворник",
        other_titles=["Хирург"],
        is_new_row=True,
    )

    assert page.expand_clicks >= 1
    assert checkboxes["Дворник"].is_checked() is True


def test_missing_target_checkbox_always_fails_closed(monkeypatch):
    from hhru_bot.experience import ResumePanelReconciliationError

    checkboxes = {
        # Only the OTHER resume renders; the target's row is absent.
        "Хирург": _AsyncCheckbox(),
    }
    page = _page_with_expanded_panel(monkeypatch, checkboxes)

    with pytest.raises(ResumePanelReconciliationError):
        _reconcile_experience_resume_panel(
            page,
            target_title="Дворник",
            other_titles=["Хирург"],
            is_new_row=True,
        )


def test_missing_other_checkbox_fails_closed_when_panel_never_expanded(monkeypatch):
    """Without a confirmed expand the panel should already be fully rendered;
    a missing checkbox there is unconfirmed drift (#782), not a known hh.ru
    omission."""
    from hhru_bot.experience import ResumePanelReconciliationError

    checkboxes = {"Дворник": _AsyncCheckbox()}  # 'Хирург' absent, no expand control
    page = _AsyncPanelPage(checkboxes)

    with pytest.raises(ResumePanelReconciliationError):
        _reconcile_experience_resume_panel(
            page,
            target_title="Дворник",
            other_titles=["Хирург"],
            is_new_row=True,
        )
