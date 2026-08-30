"""The two education editors are different screens (#773).

Live probe 2026-08-30 showed the additional-education form shares neither its
buttons nor its field addressing with the primary one:

* primary    -> /profile/edit/primaryEducation        -> ``profile-layout-*``
  buttons, ``profile-education-*-input`` fields;
* additional -> /resume/edit/<id>/additionalEducation -> ``resume-partial-edit-*``
  buttons, and inputs with NO ``data-qa`` at all, bound only through
  ``aria-labelledby`` + ``<label>`` (Magritte).

These tests pin that split so a future edit cannot collapse the two shapes back
into one set of selectors, and pin the fail-closed behaviour of the
label-addressed path.
"""

from __future__ import annotations

import pytest

from hhru_bot import resume_education
from hhru_bot.browser import PageStateIndeterminate
from hhru_bot.config_sections.education import EducationRecord
from hhru_bot.resume_education import _edit_block, _field_locator

pytestmark = pytest.mark.unit


class RecordingLocator:
    def __init__(self, page, selector, count=1):
        self._page = page
        self._selector = selector
        self._count = count
        self.first = self

    def count(self):
        return self._count

    def fill(self, value):
        self._page.filled.append((self._selector, value))

    def click(self):
        self._page.clicked.append(self._selector)

    def or_(self, other):  # noqa: ARG002
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        pass


class LabelPage:
    """Fake page that only resolves fields the live additional form exposes.

    ``locator`` deliberately reports count=0 for every ``data-qa`` field
    selector: on the real additional-education form those attributes do not
    exist, so a fake that answered count=1 would hide exactly the drift this
    change fixes.
    """

    def __init__(self, *, labels=None, label_count=1, resume_id="RID"):
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.url = f"https://hh.ru/resume/{resume_id}"
        self._labels = (
            labels if labels is not None else set(resume_education._ADDITIONAL_LABELS.values())
        )
        self._label_count = label_count

    def locator(self, selector: str):
        if selector.startswith("[data-qa='resume-edit-button-"):
            return RecordingLocator(self, selector, count=1)
        if selector.startswith("[data-qa='profile-education-"):
            return RecordingLocator(self, selector, count=0)
        return RecordingLocator(self, selector, count=1)

    def get_by_label(self, text: str, *, exact: bool | None = None):  # noqa: ARG002
        count = self._label_count if text in self._labels else 0
        return RecordingLocator(self, f"label:{text}", count=count)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        pass


def test_additional_and_primary_use_different_save_and_cancel_controls():
    assert resume_education.SAVE_BUTTON != resume_education.ADDITIONAL_SAVE_BUTTON
    assert resume_education.CANCEL_BUTTON != resume_education.ADDITIONAL_CANCEL_BUTTON
    assert resume_education.ADDITIONAL_SAVE_BUTTON == "[data-qa='resume-partial-edit-save']"
    assert resume_education.ADDITIONAL_CANCEL_BUTTON == "[data-qa='resume-partial-edit-cancel']"


def test_additional_fields_are_addressed_by_visible_label():
    page = LabelPage()
    locator = _field_locator(page, "institution", additional=True)
    locator.fill("МГУ")
    assert page.filled == [("label:Название", "МГУ")]


def test_primary_fields_are_still_addressed_by_data_qa():
    class PrimaryPage(LabelPage):
        def locator(self, selector: str):
            return RecordingLocator(self, selector, count=1)

    page = PrimaryPage()
    locator = _field_locator(page, "institution", additional=False)
    locator.fill("МГУ")
    assert page.filled == [("[data-qa='profile-education-university-input']", "МГУ")]


@pytest.mark.parametrize("label_count", [0, 2])
def test_ambiguous_label_fails_closed(label_count):
    """A missing or duplicated label must stop the write, never guess a field."""
    page = LabelPage(label_count=label_count)
    with pytest.raises(PageStateIndeterminate):
        _field_locator(page, "institution", additional=True)


def test_additional_block_dry_run_fills_by_label_and_cancels_partial_editor(monkeypatch):
    monkeypatch.setattr(resume_education, "open_hydrated_resume_editor", lambda *a, **k: None)
    page = LabelPage()
    record = EducationRecord(
        institution="Курсы",
        level="",
        faculty="",
        organization="Организация",
        specialty="Специализация",
        year="2020",
    )

    result = _edit_block(page, [record], additional=True, dry_run=True, resume_id="RID")

    assert result.success, result.reason
    # Filled through labels, not through any data-qa selector.
    assert page.filled == [
        ("label:Название", "Курсы"),
        ("label:Проводившая организация", "Организация"),
        ("label:Специализация", "Специализация"),
        ("label:Год окончания", "2020"),
    ]
    # Dry run must leave the editor through the partial-edit cancel button.
    assert page.clicked == [resume_education.ADDITIONAL_CANCEL_BUTTON]


def test_additional_block_reports_unresolvable_label_without_saving(monkeypatch):
    monkeypatch.setattr(resume_education, "open_hydrated_resume_editor", lambda *a, **k: None)
    page = LabelPage(labels={"Название"})
    record = EducationRecord(
        institution="Курсы",
        level="",
        faculty="",
        organization="Организация",
        specialty="",
        year="",
    )

    result = _edit_block(page, [record], additional=True, dry_run=False, resume_id="RID")

    assert not result.success
    assert "Проводившая организация" in result.reason
    assert result.saved == 0
    assert not result.uncertain
    assert resume_education.ADDITIONAL_SAVE_BUTTON not in page.clicked
