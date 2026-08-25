import pytest

from hhru_bot import experience

pytestmark = pytest.mark.unit


class _Locator:
    def count(self):
        return 0


class _Page:
    def locator(self, selector):
        assert selector is not None
        return _Locator()


def test_edit_experience_fails_closed_when_add_selector_is_unavailable(monkeypatch):
    monkeypatch.setattr(experience, "open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr(experience, "EXPERIENCE_ADD_BUTTON", None)

    results = experience.edit_experience_on_hh(
        _Page(),
        "resume-1",
        experience.ExperiencePlan([experience.ExperienceEntry(company="Acme")]),
        dry_run=True,
    )

    assert results == [
        experience.ExperienceResult("строка опыта 0: add-триггер не подтверждён однозначно")
    ]
