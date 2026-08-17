import pytest
from playwright.sync_api import Error as PlaywrightError

import hhru_bot.publish_resume as publish
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.publish_resume import parse_resume_state

pytestmark = pytest.mark.integration
RESUME_ID = "a" * 38


def _resume():
    return ResumeConfig(
        id="python",
        resume_url=f"https://hh.ru/resume/{RESUME_ID}",
        search=SearchFilters(text="python"),
    )


class _Locator:
    def __init__(self, page, count=1):
        self.page = page
        self._count = count

    def count(self):
        return self._count

    def wait_for(self, timeout=None):
        return None

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.page.clicked += 1
        self.page.markup = self.page.after_markup

    def inner_text(self):
        return "Изменить видимость"

    def or_(self, other):
        # Real Playwright's .or_() keeps clicking the actual matched element —
        # preserve the concrete locator type so error-injecting subclasses
        # (e.g. _ErrorLocator) aren't silently swapped for a plain _Locator.
        winner = self if self._count >= other._count else other
        return type(winner)(self.page, max(self._count, other._count))


class _Page:
    def __init__(self, markup, publish_count=1):
        self.markup = markup
        self.after_markup = _markup(status="new", isSearchable=True)
        self.publish_count = publish_count
        self.clicked = 0
        self.reloaded = 0
        self.url = f"https://hh.ru/resume/{RESUME_ID}"

    def content(self):
        return self.markup

    def locator(self, selector):
        if "Опубликовать" in selector or "resume-publish" in selector:
            return _Locator(self, self.publish_count)
        return _Locator(self, 0)

    def wait_for_timeout(self, timeout):
        return None

    def reload(self, wait_until=None):
        self.reloaded += 1
        self.markup = self.after_markup


def _markup(**overrides):
    values = {
        "status": "not_finished",
        "isSearchable": False,
        "canPublishOrUpdate": True,
    }
    values.update(overrides)
    searchable = str(values["isSearchable"]).lower()
    can_publish = str(values["canPublishOrUpdate"]).lower()
    return f'{{"id":"{RESUME_ID}","status":"{values["status"]}","isSearchable":{searchable},"canPublishOrUpdate":{can_publish}}}'


def _run(page, monkeypatch, *, preserve_url=False):
    goto = (lambda page, url: None) if preserve_url else lambda page, url: setattr(page, "url", url)
    monkeypatch.setattr(publish, "goto_hh", goto)
    monkeypatch.setattr(publish, "has_login_form", lambda page: False)
    return publish.publish_resume_on_hh(page, _resume(), dry_run=False)


def test_parse_resume_state_keeps_independent_fields():
    state = parse_resume_state(
        '{"status":"not_finished","isSearchable":false,"canPublishOrUpdate":true,'
        '"nextIncompleteScreenId":"professional_role"}'
    )
    assert state.status == "not_finished"
    assert state.is_searchable is False
    assert state.can_publish_or_update is True
    assert state.next_incomplete_screen_id == "professional_role"


def test_parse_resume_state_does_not_guess_missing_values():
    state = parse_resume_state('{"status":"not_finished"}')
    assert state.status == "not_finished"
    assert state.is_searchable is None
    assert state.can_publish_or_update is None
    assert state.next_incomplete_screen_id is None


def test_parse_resume_state_binds_all_fields_to_target_record():
    markup = (
        '[{"id":"other","status":"not_finished","isSearchable":true,'
        '"canPublishOrUpdate":true},'
        f'{{"id":"{RESUME_ID}","status":"finished","isSearchable":false,'
        '"canPublishOrUpdate":false}}]'
    )
    state = parse_resume_state(markup, RESUME_ID)
    assert state.status == "finished"
    assert state.is_searchable is False
    assert state.can_publish_or_update is False


def test_parse_resume_state_reads_page_scoped_incomplete_screen_for_target():
    markup = (
        '{"scheme":{"nextIncompleteScreenId":"professional_role"},'
        f'"resume":{{"hash":"{RESUME_ID}","status":"not_finished",'
        '"isSearchable":false,"canPublishOrUpdate":false}}}'
    )
    state = parse_resume_state(markup, RESUME_ID)
    assert state.next_incomplete_screen_id == "professional_role"


def test_publish_rejects_identity_mismatch_before_button_lookup(monkeypatch):
    page = _Page(_markup())
    page.url = "https://hh.ru/resume/" + "b" * 38
    result = _run(page, monkeypatch, preserve_url=True)
    assert not result.success
    assert "identity" in result.reason
    assert page.clicked == 0


def test_publish_rejects_missing_state_before_click(monkeypatch):
    result = _run(_Page('{"status":"not_finished"}'), monkeypatch)
    assert not result.success
    assert "не подтверждено" in result.reason


def test_publish_rejects_finished_resume_and_does_not_click(monkeypatch):
    page = _Page(_markup(status="finished"))
    result = _run(page, monkeypatch)
    assert not result.success
    assert "опубликовано" in result.reason
    assert page.clicked == 0


@pytest.mark.parametrize("status", ["new", "approved", "modified"])
def test_publish_rejects_any_searchable_resume_and_does_not_click(monkeypatch, status):
    page = _Page(_markup(status=status, isSearchable=True, canPublishOrUpdate=False))
    result = _run(page, monkeypatch)
    assert not result.success
    assert result.reason == "резюме уже опубликовано"
    assert page.clicked == 0


def test_publish_rejects_can_publish_false(monkeypatch):
    page = _Page(_markup(canPublishOrUpdate=False)[:-1] + ',"nextIncompleteScreenId":"professional_role"}')
    result = _run(page, monkeypatch)
    assert not result.success
    assert "canPublishOrUpdate=False" in result.reason
    assert "nextIncompleteScreenId=professional_role" in result.reason
    assert page.clicked == 0


def test_publish_rejects_missing_or_ambiguous_button(monkeypatch):
    missing = _Page(_markup(), publish_count=0)
    ambiguous = _Page(_markup(), publish_count=2)
    assert not _run(missing, monkeypatch).success
    assert not _run(ambiguous, monkeypatch).success
    assert missing.clicked == ambiguous.clicked == 0


def test_publish_dry_run_never_clicks(monkeypatch):
    page = _Page(_markup())
    monkeypatch.setattr(publish, "goto_hh", lambda page, url: setattr(page, "url", url))
    monkeypatch.setattr(publish, "has_login_form", lambda page: False)
    result = publish.publish_resume_on_hh(page, _resume(), dry_run=True)
    assert result.success
    assert page.clicked == 0


def test_publish_requires_positive_finished_signal(monkeypatch):
    page = _Page(_markup())
    page.after_markup = _markup(status="not_finished", isSearchable=False)
    monkeypatch.setattr(publish, "PUBLISH_TIMEOUT_MS", 1)
    result = _run(page, monkeypatch)
    assert not result.success
    assert "не подтверждена" in result.reason
    assert page.clicked == 1
    # #219 (по аналогии с #176/#207): клик состоялся, но подтверждения нет —
    # это серая зона, не обычный failed, иначе пользователь может бездумно
    # повторить --force поверх уже состоявшейся публикации.
    assert result.uncertain is True


def test_publish_succeeds_only_after_searchable_signal(monkeypatch):
    page = _Page(_markup())
    result = _run(page, monkeypatch)
    assert result.success
    assert page.clicked == 1
    assert result.uncertain is False


def test_publish_marks_uncertain_on_click_error(monkeypatch):
    class _ErrorLocator(_Locator):
        def click(self, timeout=None):
            raise PlaywrightError("navigation interrupted")

    class _ErrorPage(_Page):
        def locator(self, selector):
            if "Опубликовать" in selector or "resume-publish" in selector:
                return _ErrorLocator(self, self.publish_count)
            return _Locator(self, 0)

    page = _ErrorPage(_markup())
    result = _run(page, monkeypatch)
    assert not result.success
    assert result.uncertain is True
    assert "ошибка клика" in result.reason


def test_publish_pre_click_rejection_is_not_uncertain(monkeypatch):
    page = _Page(_markup(status="finished"))
    result = _run(page, monkeypatch)
    assert not result.success
    assert result.uncertain is False
