"""Браузерная логика wizard-next: резолюция экрана, гейт #991, защищённый клик.

Живой контекст (#1010): флаг nextIncompleteScreenId двигается только сабмитом
экранов визарда; NEXT на последнем экране hh.ru публикует резюме сам (#900).
Браузер не запускается: навигация/гидрация/аутентичность подменяются, страница —
дубль с управляемым исходом.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hhru_bot.resume_wizard as rw
from hhru_bot.resume_state import ResumeState
from hhru_bot.selector_groups.resume_page import RESUME_CREATION_NEXT

pytestmark = pytest.mark.unit

RESUME_ID = "a" * 38


def _markup(
    rid=RESUME_ID,
    *,
    status="not_finished",
    searchable=False,
    next_screen="educations",
    publishable=True,
):
    next_value = "null" if next_screen is None else f'"{next_screen}"'
    return (
        "<html><script>{"
        f'"resume": {{"id": "{rid}", "status": "{status}", '
        f'"isSearchable": {str(searchable).lower()}, '
        f'"canPublishOrUpdate": {str(publishable).lower()}, '
        f'"nextIncompleteScreenId": {next_value}'
        "}}</script></html>"
    )


class _NextButton:
    def __init__(self, page):
        self._page = page

    def count(self):
        return self._page.next_count

    @property
    def first(self):
        return self

    def inner_text(self):
        return "Сохранить и продолжить"

    def click(self):
        self._page.clicks += 1
        # #913: навигация могла состояться ДО падения клика — URL меняется первым.
        self._page.url = self._page.final_url
        if self._page.click_error is not None:
            raise rw.PlaywrightError(self._page.click_error)


class _WizardPage:
    def __init__(
        self,
        markup,
        *,
        url="",
        final_url=None,
        hydrated=True,
        next_count=1,
        click_error=None,
        goto_override=None,
    ):
        self.markup = markup
        self.url = url
        self.final_url = final_url
        self.hydrated = hydrated
        self.next_count = next_count
        self.click_error = click_error
        # #999: hh.ru может редиректить goto на другой экран визарда.
        self.goto_override = goto_override
        self.clicks = 0

    def content(self):
        return self.markup

    def locator(self, selector):
        assert selector == RESUME_CREATION_NEXT
        return _NextButton(self)

    def wait_for_url(self, predicate, *, wait_until, timeout):  # noqa: ARG002
        if predicate(self.url):
            return None
        raise rw.PlaywrightError(f"wait_for_url: timeout {timeout}ms")


def _install_nav_stubs(monkeypatch) -> list[str]:
    """Подменяем навигацию/аутентичность: URL живёт на самом дубле страницы.

    Возвращает список тегов dump_page_html: обе fail-ветки сабмита обязаны
    дампить экран (см. save_common), тесты это проверяют.
    """
    dumps: list[str] = []

    def fake_open_confirmed(page, resume_id):
        page.url = f"https://hh.ru/resume/{resume_id}"

    def fake_goto(page, url, *, ready_selector=None):  # noqa: ARG001
        page.url = page.goto_override(url) if page.goto_override else url

    monkeypatch.setattr(rw, "open_confirmed_resume", fake_open_confirmed)
    monkeypatch.setattr(rw, "goto_hh", fake_goto)
    monkeypatch.setattr(rw, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(rw, "dismiss_cookie_banner", lambda page, **kw: None)
    monkeypatch.setattr(
        rw, "wait_for_react_hydration", lambda page, sel, *, timeout_ms: page.hydrated
    )
    monkeypatch.setattr(rw, "dump_page_html", lambda page, stem: dumps.append(stem))
    return dumps


def _resume():
    return SimpleNamespace(id="python", resume_id=RESUME_ID)


# --- resolve_target_screen: отказы до всякого клика -------------------------


def test_resolve_refuses_published_resume():
    state = ResumeState(status="finished", is_searchable=True)
    with pytest.raises(rw.WizardScreenRefused, match="уже опубликовано"):
        rw.resolve_target_screen(state, None)


def test_resolve_refuses_when_no_incomplete_screens():
    state = ResumeState(status="not_finished", next_incomplete_screen_id=None)
    with pytest.raises(rw.WizardScreenRefused, match="publish-resume"):
        rw.resolve_target_screen(state, None)


def test_resolve_names_owner_for_common():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="common")
    with pytest.raises(rw.WizardScreenRefused, match="hhru common"):
        rw.resolve_target_screen(state, None)


def test_resolve_names_owner_for_professional_role():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="professional_role")
    with pytest.raises(rw.WizardScreenRefused, match="resume-position"):
        rw.resolve_target_screen(state, None)


def test_resolve_refuses_screen_without_owner():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="recommendations")
    with pytest.raises(rw.WizardScreenRefused, match="нет CLI-владельца"):
        rw.resolve_target_screen(state, None)


def test_resolve_refuses_mismatched_explicit_screen():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="educations")
    with pytest.raises(rw.WizardScreenRefused, match="ждёт экран «educations».*«keyskills»"):
        rw.resolve_target_screen(state, "keyskills")


def test_resolve_takes_current_screen_by_default():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="keyskills")
    assert rw.resolve_target_screen(state, None) == "keyskills"


def test_resolve_accepts_matching_explicit_screen():
    state = ResumeState(status="not_finished", next_incomplete_screen_id="educations")
    assert rw.resolve_target_screen(state, "educations") == "educations"


# --- submit_wizard_screen: гейт гидрации и защищённый клик ------------------


def test_submit_success_calls_before_click_once(monkeypatch):
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(),
        final_url="https://hh.ru/profile/resume/keyskills?resume=" + RESUME_ID,
    )
    clicks = []
    result = rw.submit_wizard_screen(
        page, _resume(), "educations", before_click=lambda: clicks.append(1)
    )
    assert result.success and result.acted and not result.uncertain
    assert page.clicks == 1 and len(clicks) == 1


def test_submit_without_hydration_is_honest_failed(monkeypatch):
    """#991: не гидратирован — клик не отправлялся; это failed, не uncertain."""
    dumps = _install_nav_stubs(monkeypatch)
    page = _WizardPage(_markup(), hydrated=False)
    clicks = []
    result = rw.submit_wizard_screen(
        page, _resume(), "educations", before_click=lambda: clicks.append(1)
    )
    assert not result.success and not result.acted and not result.uncertain
    assert "клик не отправлялся" in result.reason
    assert page.clicks == 0 and clicks == []
    assert dumps == ["wizard_next_failure"]


def test_submit_swallows_click_error_when_navigation_happened(monkeypatch):
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(),
        final_url="https://hh.ru/profile/resume/keyskills?resume=" + RESUME_ID,
        click_error="intercepted by overlay",
    )
    result = rw.submit_wizard_screen(page, _resume(), "educations")
    assert result.success and result.acted and page.clicks == 1


def test_submit_is_uncertain_when_url_never_leaves_screen(monkeypatch):
    dumps = _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(), final_url=f"https://hh.ru/profile/resume/educations?resume={RESUME_ID}"
    )
    result = rw.submit_wizard_screen(page, _resume(), "educations")
    assert not result.success and result.acted and result.uncertain
    assert dumps == ["wizard_next_failure"]


def test_uncertain_reason_carries_click_error(monkeypatch):
    """#990: текст падения клика доходит до reason — иначе «дошёл ли клик»
    недиагностируем (uncertain блокирует повтор, reason в actions —
    единственная улика)."""
    dumps = _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(),
        final_url=f"https://hh.ru/profile/resume/educations?resume={RESUME_ID}",
        click_error="intercepted by overlay",
    )
    result = rw.submit_wizard_screen(page, _resume(), "educations")
    assert not result.success and result.acted and result.uncertain
    assert "ошибка клика: intercepted by overlay" in result.reason
    assert dumps == ["wizard_next_failure"]


def test_submit_refuses_when_wizard_stands_on_other_screen(monkeypatch):
    """#999: редирект ушёл на чужой экран — отказ ДО before_click."""
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(),
        url=f"https://hh.ru/profile/resume/common?resume={RESUME_ID}",
        goto_override=lambda _u: f"https://hh.ru/profile/resume/common?resume={RESUME_ID}",
    )
    clicks = []
    with pytest.raises(rw.WizardScreenRefused, match="визард стоит на «common»"):
        rw.submit_wizard_screen(
            page, _resume(), "educations", before_click=lambda: clicks.append(1)
        )
    assert page.clicks == 0 and clicks == []


# --- inspect_wizard_screen: read-only сверка для --dry-run ------------------


def test_inspect_reports_next_button_label(monkeypatch):
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(_markup(), url=f"https://hh.ru/profile/resume/educations?resume={RESUME_ID}")
    assert rw.inspect_wizard_screen(page, RESUME_ID, "educations") == "Сохранить и продолжить"


def test_inspect_refuses_ambiguous_next(monkeypatch):
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(),
        url=f"https://hh.ru/profile/resume/educations?resume={RESUME_ID}",
        next_count=2,
    )
    with pytest.raises(rw.WizardScreenRefused, match="найдена 2 раз"):
        rw.inspect_wizard_screen(page, RESUME_ID, "educations")
