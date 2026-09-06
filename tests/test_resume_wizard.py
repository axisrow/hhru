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
from hhru_bot.selector_groups.resume_page import RESUME_CREATION_NEXT, RESUME_PARTIAL_EDIT_SAVE

pytestmark = pytest.mark.unit

RESUME_ID = "a" * 38
EDITOR_URL = f"https://hh.ru/resume/edit/{RESUME_ID}/skillsLevels?fromBlock=keySkills"


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
        return self._page.button_label

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
        button_selector=RESUME_CREATION_NEXT,
        button_label="Сохранить и продолжить",
    ):
        self.markup = markup
        self.url = url
        self.final_url = final_url
        self.hydrated = hydrated
        self.next_count = next_count
        self.click_error = click_error
        # #999: hh.ru может редиректить goto на другой экран визарда.
        self.goto_override = goto_override
        # У экрана один сабмит: NEXT визарда или Save редактора skill_levels.
        self.button_selector = button_selector
        self.button_label = button_label
        self.clicks = 0

    def content(self):
        return self.markup

    def locator(self, selector):
        assert selector == self.button_selector
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


def test_resolve_accepts_skill_levels():
    """skill_levels — валидный экран визарда, а не «нет владельца»."""
    state = ResumeState(status="not_finished", next_incomplete_screen_id="skill_levels")
    assert rw.resolve_target_screen(state, "skill_levels") == "skill_levels"


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


# --- skill_levels: экран живёт в редакторе уровней, сабмит — его Save --------


def test_submit_skill_levels_clicks_editor_save(monkeypatch):
    """skill_levels открывается маршрутом редактора /resume/edit/{id}/skillsLevels
    (#813) и сабмитится его Save-кнопкой, не NEXT визарда. Успех требует
    readback'а: флаг ушёл с skill_levels (#1014)."""
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="experience"),
        url=EDITOR_URL,
        final_url=f"https://hh.ru/resume/{RESUME_ID}",
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
        button_label="Сохранить",
    )
    clicks = []
    result = rw.submit_wizard_screen(
        page, _resume(), "skill_levels", before_click=lambda: clicks.append(1)
    )
    assert result.success and result.acted and not result.uncertain
    assert page.clicks == 1 and len(clicks) == 1


def test_submit_skill_levels_fails_when_flag_stays(monkeypatch):
    """#1014, живой факт 2026-09-07: Save редактора уходит с маршрута, но флаг
    не двигает — это честный failed (исход известен), не uncertain и не
    ложный успех."""
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="skill_levels"),
        url=EDITOR_URL,
        final_url=f"https://hh.ru/resume/{RESUME_ID}",
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
    )
    result = rw.submit_wizard_screen(page, _resume(), "skill_levels")
    assert not result.success and result.acted and not result.uncertain
    assert "nextIncompleteScreenId не двигается" in result.reason
    assert "«Указать уровни»" in result.reason


def test_submit_skill_levels_refuses_when_redirected_away(monkeypatch):
    """#999-семейство: hh.ru ушёл с маршрута редактора (флаг уже снят) — отказ
    ДО before_click."""
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="skill_levels"),
        goto_override=lambda _u: f"https://hh.ru/resume/{RESUME_ID}",
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
    )
    clicks = []
    with pytest.raises(rw.WizardScreenRefused, match="не открыт"):
        rw.submit_wizard_screen(
            page, _resume(), "skill_levels", before_click=lambda: clicks.append(1)
        )
    assert page.clicks == 0 and clicks == []


def test_submit_skill_levels_is_uncertain_when_url_stays_in_editor(monkeypatch):
    """Save без перехода с редактора — та же семантика uncertain (#176):
    клик мог уйти, дамп обязателен."""
    dumps = _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="skill_levels"),
        url=EDITOR_URL,
        final_url=EDITOR_URL,
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
    )
    result = rw.submit_wizard_screen(page, _resume(), "skill_levels")
    assert not result.success and result.acted and result.uncertain
    assert dumps == ["wizard_next_failure"]


def test_submit_skill_levels_requires_editor_save_button(monkeypatch):
    """На экране skill_levels нет NEXT визарда — поиск идёт по Save редактора;
    0 совпадений = экран не опознан, отказ до клика."""
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="skill_levels"),
        url=EDITOR_URL,
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
        next_count=0,
    )
    with pytest.raises(rw.WizardScreenRefused, match="найдена 0 раз"):
        rw.submit_wizard_screen(page, _resume(), "skill_levels")


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


def test_inspect_skill_levels_reports_editor_save_label(monkeypatch):
    _install_nav_stubs(monkeypatch)
    page = _WizardPage(
        _markup(next_screen="skill_levels"),
        url=EDITOR_URL,
        button_selector=RESUME_PARTIAL_EDIT_SAVE,
        button_label="Сохранить",
    )
    assert rw.inspect_wizard_screen(page, RESUME_ID, "skill_levels") == "Сохранить"


def test_is_publishing_screen_only_last_supported_screen():
    """#1012: прогноз публикации — только последний экран SUPPORTED_SCREENS
    (#900, прогоны #1009 и «Повар» 2026-09-06). skill_levels — промежуточный,
    после него всегда остаётся experience (живой факт 2026-09-07)."""
    assert rw.is_publishing_screen("experience") is True
    assert rw.is_publishing_screen("educations") is False
    assert rw.is_publishing_screen("keyskills") is False
    assert rw.is_publishing_screen("skill_levels") is False
    assert rw.is_publishing_screen("common") is False
