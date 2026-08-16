import hhru_bot.publish_resume as publish
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.publish_resume import parse_resume_state

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
        return _Locator(self.page, max(self._count, other._count))


class _Page:
    def __init__(self, markup, publish_count=1):
        self.markup = markup
        self.after_markup = markup.replace('"status":"not_finished"', '"status":"finished"')
        self.publish_count = publish_count
        self.clicked = 0
        self.url = f"https://hh.ru/resume/{RESUME_ID}"

    def content(self):
        return self.markup

    def locator(self, selector):
        if "Опубликовать" in selector or "resume-publish" in selector:
            return _Locator(self, self.publish_count)
        return _Locator(self, 0)

    def wait_for_timeout(self, timeout):
        return None


def _markup(**overrides):
    values = {
        "status": "not_finished",
        "isSearchable": False,
        "canPublishOrUpdate": True,
    }
    values.update(overrides)
    searchable = str(values["isSearchable"]).lower()
    can_publish = str(values["canPublishOrUpdate"]).lower()
    return f'{{"status":"{values["status"]}","isSearchable":{searchable},"canPublishOrUpdate":{can_publish}}}'


def _run(page, monkeypatch):
    monkeypatch.setattr(publish, "goto_hh", lambda page, url: setattr(page, "url", url))
    monkeypatch.setattr(publish, "has_login_form", lambda page: False)
    return publish.publish_resume_on_hh(page, _resume(), dry_run=False)


def test_parse_resume_state_keeps_independent_fields():
    state = parse_resume_state(
        '{"status":"not_finished","isSearchable":false,"canPublishOrUpdate":true}'
    )
    assert state.status == "not_finished"
    assert state.is_searchable is False
    assert state.can_publish_or_update is True


def test_parse_resume_state_does_not_guess_missing_values():
    state = parse_resume_state('{"status":"not_finished"}')
    assert state.status == "not_finished"
    assert state.is_searchable is None
    assert state.can_publish_or_update is None


def test_publish_rejects_identity_mismatch_before_button_lookup(monkeypatch):
    page = _Page(_markup())
    page.url = "https://hh.ru/resume/" + "b" * 38
    result = _run(page, monkeypatch)
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


def test_publish_rejects_can_publish_false(monkeypatch):
    page = _Page(_markup(canPublishOrUpdate=False))
    result = _run(page, monkeypatch)
    assert not result.success
    assert "canPublishOrUpdate=False" in result.reason
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
    page.after_markup = _markup(status="not_finished")
    result = _run(page, monkeypatch)
    assert not result.success
    assert "не подтверждена" in result.reason
    assert page.clicked == 1


def test_publish_succeeds_only_after_finished_signal(monkeypatch):
    page = _Page(_markup())
    result = _run(page, monkeypatch)
    assert result.success
    assert page.clicked == 1
