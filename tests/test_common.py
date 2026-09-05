"""Unit tests for the simple common-screen editor."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hhru_bot.common as common
from hhru_bot.common import (
    CommonValues,
    apply_common,
    merge_prefilled,
    missing_required,
    read_common,
)

pytestmark = pytest.mark.unit


def test_common_values_exposes_only_first_slice():
    values = CommonValues(first_name="Ada", last_name="Lovelace", gender="female")
    assert values.provided() == {
        "firstName": "Ada",
        "lastName": "Lovelace",
        "gender": "female",
    }


def test_merge_prefilled_keeps_hh_prefilled_fields_untouched():
    requested = CommonValues(first_name="Ada", last_name="Lovelace", phone="+7")
    current = CommonValues(first_name="Иван", last_name="", phone="+7999")
    effective, skipped = merge_prefilled(requested, current)
    # Поля, которые hh.ru предзаполнил из профиля, не затираются (#982).
    assert effective.provided() == {"lastName": "Lovelace"}
    assert dict(skipped) == {"first_name": "Иван", "phone": "+7999"}


def test_merge_prefilled_treats_blank_and_none_as_empty():
    requested = CommonValues(first_name="Ada", schedule=["full_day"])
    current = CommonValues(first_name="   ", schedule=[], citizenship=None)
    effective, skipped = merge_prefilled(requested, current)
    assert effective.provided() == {
        "firstName": "Ada",
        "schedule": ["full_day"],
    }
    assert skipped == []


def test_missing_required_lists_only_blank_fields():
    current = CommonValues(first_name="Иван", last_name="", phone="+7999")
    assert missing_required(current) == ["last_name", "birthday", "gender", "citizenship"]


def test_apply_common_fills_inputs_and_selects_gender():
    page = MagicMock()
    locators = {}
    for selector in (
        common.FIRST_NAME,
        common.LAST_NAME,
        common.BIRTHDAY,
        common.GENDER,
        common.PHONE,
    ):
        locators[selector] = MagicMock()
        locators[selector].count.return_value = 1
    page.locator.side_effect = lambda selector: locators[selector]

    apply_common(
        page,
        CommonValues(
            first_name="Ada",
            last_name="Lovelace",
            birthday="1815-12-10",
            gender="female",
            phone="+7",
        ),
    )

    locators[common.FIRST_NAME].first.fill.assert_called_once_with("Ada")
    locators[common.LAST_NAME].first.fill.assert_called_once_with("Lovelace")
    locators[common.BIRTHDAY].first.fill.assert_called_once_with("1815-12-10")
    locators[common.PHONE].first.fill.assert_called_once_with("+7")
    locators[common.GENDER].first.select_option.assert_called_once_with("female")


@pytest.mark.browser_unit
def test_apply_common_uses_exact_tree_leaf_identity(tmp_path):
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    fixture = Path(__file__).parent / "fixtures" / "common_catalogs.html"
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(fixture.read_text(encoding="utf-8"))
    try:
        apply_common(
            page,
            CommonValues(metro=["Маяковская"]),
        )
        apply_common(page, CommonValues(area="Москва", citizenship=["Россия"]))
        assert page.locator(common.TREE_MODAL).is_hidden()
        assert page.locator("#selected").inner_text() == "Москва|Маяковская|Россия"
        apply_common(page, CommonValues(metro=[]))
        assert page.locator(f"{common.TREE_OPTION}[aria-selected='true']").count() == 0
    finally:
        browser.close()
        playwright.stop()


def test_read_common_fails_closed_on_ambiguous_selector():
    page = MagicMock()
    field = MagicMock()
    field.count.return_value = 2
    page.locator.return_value = field

    with pytest.raises(RuntimeError, match="не подтверждено однозначно"):
        read_common(page)


def _command_harness(monkeypatch, tmp_path, *, current, page=None):
    """Общий mock-стенд команды common без браузера (#982)."""
    from hhru_bot import browser, config
    from hhru_bot.commands import _common
    from hhru_bot.commands import common as command
    from hhru_bot.config import bare_resume

    resume = bare_resume("00001")
    fake_config = SimpleNamespace(storage_state_file="session.json", user_agent=None)

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return page if page is not None else MagicMock()

    monkeypatch.setattr(config, "load_config_or_exit", lambda _path: fake_config)
    monkeypatch.setattr(browser, "launch_context", lambda *_args, **_kwargs: Context())
    monkeypatch.setattr(_common, "resolve_resume", lambda *_args, **_kwargs: resume)
    monkeypatch.setattr(common, "open_common_form", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(common, "read_common", lambda *_page: current)
    return command, resume


def _args(tmp_path, **overrides):
    base = dict(
        config="config.yaml",
        history=str(tmp_path / "history.db"),
        resume="00001",
        first_name=None,
        last_name=None,
        birthday=None,
        gender=None,
        phone=None,
        area=None,
        metro=None,
        citizenship=None,
        work_ticket=None,
        relocation=None,
        schedule=None,
        employment=None,
        work_format=None,
        business_trip=None,
        show=False,
        dry_run=True,
        force=False,
        headless=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_common_show_prints_current_values_without_saving(monkeypatch, tmp_path, capsys):
    current = CommonValues(
        first_name="Иван", last_name="Иванов", phone="+7999", birthday="", gender="", area=""
    )
    command, _resume = _command_harness(monkeypatch, tmp_path, current=current)
    saved = []
    monkeypatch.setattr(common, "save_common", lambda *_a, **_k: saved.append(1))

    assert command._run(_args(tmp_path, show=True), MagicMock()) is False
    out = capsys.readouterr().out
    assert "Иван" in out
    assert "заполнено (обязательное)" in out
    assert "пусто" in out
    assert "read-only" in out
    assert saved == []


def test_common_auto_mode_fails_closed_on_missing_required(monkeypatch, tmp_path, capsys):
    current = CommonValues(first_name="Иван", last_name="", phone="")
    command, _resume = _command_harness(monkeypatch, tmp_path, current=current)
    saved = []
    monkeypatch.setattr(common, "save_common", lambda *_a, **_k: saved.append(1))

    assert command._run(_args(tmp_path), MagicMock()) is True
    out = capsys.readouterr().out
    assert "last_name" in out
    assert "Ничего не сохранено" in out
    assert saved == []


def test_common_auto_mode_saves_prefilled_when_required_complete(monkeypatch, tmp_path):
    current = CommonValues(
        first_name="Иван",
        last_name="Иванов",
        birthday="01.01.1990",
        gender="male",
        phone="+7999",
        citizenship=["Россия"],
    )
    command, _resume = _command_harness(monkeypatch, tmp_path, current=current)
    monkeypatch.setattr(command, "confirm_write", lambda *_a, **_k: True)
    captured = {}

    def fake_save(_page, values, **_kwargs):
        captured["values"] = values
        return common.CommonResult(True, "поля common сохранены", True)

    monkeypatch.setattr(common, "save_common", fake_save)

    assert command._run(_args(tmp_path, dry_run=False), MagicMock()) is False
    assert captured["values"].provided() == {}


def test_common_write_skips_prefilled_fields(monkeypatch, tmp_path, capsys):
    current = CommonValues(first_name="Иван", last_name="", phone="+7999")
    command, _resume = _command_harness(monkeypatch, tmp_path, current=current)
    captured = {}

    def fake_save(_page, values, **_kwargs):
        captured["values"] = values
        return common.CommonResult(True, "поля common сохранены", True)

    monkeypatch.setattr(common, "save_common", fake_save)

    assert (
        command._run(
            _args(tmp_path, first_name="Пётр", last_name="Петров", dry_run=True), MagicMock()
        )
        is False
    )
    out = capsys.readouterr().out
    assert "уже заполнено на hh.ru" in out
    # last_name был пуст и передан — попадает в план заполнения.
    assert "lastName" in out


def test_common_explicit_fields_all_prefilled_skips_save(monkeypatch, tmp_path, capsys):
    current = CommonValues(first_name="Иван", phone="+7999")
    command, _resume = _command_harness(monkeypatch, tmp_path, current=current)
    saved = []
    monkeypatch.setattr(common, "save_common", lambda *_a, **_k: saved.append(1))

    result = command._run(_args(tmp_path, first_name="Пётр", dry_run=False), MagicMock())
    assert result is False
    out = capsys.readouterr().out
    assert "заполнять нечего" in out
    assert "Обязательные поля common пусты" not in out
    assert saved == []


def test_common_preserves_expired_session_classification(monkeypatch, tmp_path):
    from hhru_bot import browser, config
    from hhru_bot.commands import common as command
    from hhru_bot.config import bare_resume

    resume = bare_resume("00001")
    fake_config = SimpleNamespace(storage_state_file="session.json", user_agent=None)

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return MagicMock()

    monkeypatch.setattr(config, "load_config_or_exit", lambda _path: fake_config)
    monkeypatch.setattr(command, "confirm_write", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser, "launch_context", lambda *_args, **_kwargs: Context())
    from hhru_bot.commands import _common

    monkeypatch.setattr(_common, "resolve_resume", lambda *_args, **_kwargs: resume)
    monkeypatch.setattr(
        common,
        "open_common_form",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(browser.NotAuthenticated("expired")),
    )

    args = SimpleNamespace(
        config="config.yaml",
        history=str(tmp_path / "history.db"),
        resume="00001",
        first_name="Ada",
        last_name=None,
        birthday=None,
        gender=None,
        phone=None,
        dry_run=True,
        force=False,
        headless=True,
    )
    with pytest.raises(browser.NotAuthenticated, match="expired"):
        command._run(args, MagicMock())


def test_common_uncertain_marker_blocks_repeat(monkeypatch, tmp_path, capsys):
    """Ревью PR #986: uncertain-маркер edit_common (его пишет и этот seam, и
    create-resume --fill-common) гейтит повтор команды для того же resume_id —
    fail-closed инвариант #176/#476, раньше гейта не было вовсе."""
    from hhru_bot import browser, config
    from hhru_bot.commands import _common
    from hhru_bot.commands import common as command
    from hhru_bot.config import bare_resume
    from hhru_bot.history import History

    resume = bare_resume("00001")
    history = History(tmp_path / "history.db")
    history.record_action(
        resume.resume_id, resume.resume_id, "edit_common", "uncertain", "клик мог уйти"
    )
    fake_config = SimpleNamespace(storage_state_file="session.json", user_agent=None)

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):  # pragma: no cover - гейт срабатывает до браузера
            raise AssertionError("гейт обязан сработать до открытия браузера")

    monkeypatch.setattr(config, "load_config_or_exit", lambda _path: fake_config)
    monkeypatch.setattr(command, "confirm_write", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser, "launch_context", lambda *_args, **_kwargs: Context())
    monkeypatch.setattr(_common, "resolve_resume", lambda *_args, **_kwargs: resume)

    args = SimpleNamespace(
        config="config.yaml",
        history=str(tmp_path / "history.db"),
        resume="00001",
        first_name="Ada",
        last_name=None,
        birthday=None,
        gender=None,
        phone=None,
        dry_run=False,
        force=True,
        headless=True,
    )
    assert command._run(args, MagicMock()) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "uncertain" in out


def test_common_values_exposes_work_conditions():
    values = CommonValues(
        work_ticket="true",
        relocation="ready",
        schedule=["full_day"],
        employment=["full_time"],
        work_format=["remote"],
        business_trip="false",
    )
    assert values.provided() == {
        "workTicket": "true",
        "relocation": "ready",
        "schedule": ["full_day"],
        "employment": ["full_time"],
        "work_format": ["remote"],
        "businessTrip": "false",
    }


def test_apply_common_uses_exact_visible_labels_for_conditions():
    page = MagicMock()
    fields = {}
    for label in (
        common.WORK_TICKET,
        common.RELOCATION,
        common.SCHEDULE,
        common.EMPLOYMENT,
        common.WORK_FORMAT,
        common.BUSINESS_TRIP,
    ):
        field = MagicMock()
        field.count.return_value = 1
        field.evaluate.return_value = "SELECT"
        fields[label] = field
    page.get_by_label.side_effect = lambda label, exact: fields[label]
    apply_common(
        page, CommonValues(schedule=["full_day"], employment=["full_time"], work_format=["remote"])
    )
    fields[common.SCHEDULE].select_option.assert_called_once_with(["full_day"])
    fields[common.EMPLOYMENT].select_option.assert_called_once_with(["full_time"])
    fields[common.WORK_FORMAT].select_option.assert_called_once_with(["remote"])


@pytest.mark.browser_unit
def test_read_common_supports_magritte_trigger_and_replaces_selection():
    from playwright.sync_api import sync_playwright

    html = """
    <form>
      <input data-qa="resume-profile-common-name-input" value="A"/>
      <input data-qa="resume-profile-common-surname-input" value="B"/>
      <input data-qa="resume-profile-common-birthday-day-input" value="1"/>
      <input data-qa="resume-profile-common-gender-male-chip" type="radio" checked/>
      <input data-qa="resume-profile-common-gender-female-chip" type="radio"/>
      <input data-qa="resume-phone-cell_phone" value="+7"/>
      <input data-qa="resume-edit-area" value="Москва"/>
      <label id="work-ticket-label">Наличие трудовой книжки</label>
      <button aria-labelledby="work-ticket-label" type="button">Да</button>
      <label id="relocation-label">Готовность к переезду</label><button aria-labelledby="relocation-label" type="button">Готов к переезду</button>
      <label id="schedule-label">График работы</label>
      <button aria-labelledby="schedule-label" id="schedule" type="button">Полный день</button>
      <label id="employment-label">Тип занятости</label><button aria-labelledby="employment-label" type="button">Постоянная работа</button>
      <label id="format-label">Формат работы</label><button aria-labelledby="format-label" type="button">Офис</button>
      <label id="trip-label">Готовность к командировкам</label><button aria-labelledby="trip-label" type="button">Могу</button>
      <div data-qa="drop-base" hidden>
        <div role="option" aria-selected="true">Сменный график</div>
        <div role="option" aria-selected="false">Полный день</div>
      </div>
    </form>
    <script>
      document.getElementById('schedule').onclick = () => document.querySelector('[data-qa=drop-base]').hidden = false;
      document.querySelectorAll('[role=option]').forEach(o => o.onclick = e => {
        e.stopPropagation(); o.setAttribute('aria-selected', o.getAttribute('aria-selected') !== 'true');
      });
      document.onclick = e => {
        if (e.target.id !== 'schedule' && !e.target.closest('[role=option]'))
          document.querySelector('[data-qa=drop-base]').hidden = true;
      };
    </script>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        try:
            values = read_common(page)
            assert values.work_ticket == "Да"
            page.locator("[data-qa='drop-base']").evaluate("e=>e.hidden=false")
            common._set_many(
                page,
                page.locator("#schedule"),
                ["full_day"],
                common.SCHEDULE_LABELS,
            )
            assert page.locator("[role=option]").nth(0).get_attribute("aria-selected") == "false"
            assert page.locator("[role=option]").nth(1).get_attribute("aria-selected") == "true"
        finally:
            browser.close()


# --- #985: подтверждение экрана common fresh-черновика ------------------------


def _confirm_page(monkeypatch, **overrides):
    """Двойник страницы на экране common визарда (живой DOM 2026-09-06).

    Префицилл-проверка #985 переиспользует чтение #982 (read_common):
    двойник обязан поддерживать и инпуты, и magritte-контейнеры
    (birthday/citizenship), и отсутствующие необязательные labelled-поля."""
    from unittest.mock import MagicMock

    from playwright.sync_api import Error as PlaywrightError

    monkeypatch.setattr(common, "goto_hh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(common, "require_authenticated_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(common, "dismiss_cookie_banner", lambda *_args, **_kwargs: None)

    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/common?resume=00001"
    locators: dict = {}

    def field(selector, *, value="x", checked=None, text=None, count=1):
        loc = MagicMock()
        loc.count.return_value = count
        if value is not None:
            loc.first.input_value.return_value = value
        if checked is not None:
            loc.first.is_checked.return_value = checked
        if text is not None:
            # magritte-контейнер: activator-поиск внутри — пусто, значение —
            # видимый текст самого контейнера (та же деградация, что в read_common).
            loc.first.locator.return_value.count.return_value = 0
            loc.first.inner_text.return_value = text
        locators[selector] = loc
        return loc

    values = dict(
        # очевидно фейковые значения: реальные ФИО/телефон владельца в коде и
        # тестах запрещены (#828, тот же принцип что и для resume_id)
        surname="Тестов",
        name="Тест",
        phone="+7 000 000-00-00",
        male_checked=True,
        female_checked=False,
        citizenship="Россия",
        birthday_month="Января",
        birthday_year="1990",
        nav_error=None,
        click_error=False,
    )
    values.update(overrides)

    field(common.FORM, value=None)
    field(common.LAST_NAME, value=values["surname"])
    field(common.FIRST_NAME, value=values["name"])
    field(common.PHONE, value=values["phone"])
    field(common.GENDER, checked=values["male_checked"])
    field(common.GENDER_FEMALE, checked=values["female_checked"])
    field(common.BIRTHDAY, value="1")
    field(common.BIRTHDAY_MONTH, text=values["birthday_month"])
    field(common.BIRTHDAY_YEAR, text=values["birthday_year"])
    field(common.CITIZENSHIP_SELECTOR, text=values["citizenship"])
    # необязательные labelled-поля условий работы на shape визарда отсутствуют
    absent = MagicMock()
    absent.count.return_value = 0
    page.get_by_label.return_value = absent
    save = field(common.SAVE, value=None)
    if values["click_error"]:
        save.first.click.side_effect = PlaywrightError("pointer intercepted")
    if values["nav_error"] is not None:
        page.wait_for_url.side_effect = values["nav_error"]

    def locate(selector):
        if selector not in locators:
            # отсутствующее на shape визарда поле (area и т.п.) — мягкое чтение
            # #982 трактует как пустое значение, а не ошибку двойника
            locators[selector] = field(selector, count=0)
        return locators[selector]

    page.locator.side_effect = locate
    return page, save, locators


def test_confirm_common_screen_clicks_next_and_requires_url_change(monkeypatch):
    page, save, _locators = _confirm_page(monkeypatch)
    before_click = MagicMock()
    result = common.confirm_common_screen(page, "00001", before_click=before_click)
    assert result.success and result.acted and not result.uncertain
    before_click.assert_called_once_with()
    save.first.click.assert_called_once_with()
    page.wait_for_url.assert_called_once()


def test_confirm_common_screen_refuses_before_click_on_missing_prefill(monkeypatch):
    page, save, _locators = _confirm_page(monkeypatch, name="")
    before_click = MagicMock()
    result = common.confirm_common_screen(page, "00001", before_click=before_click)
    assert not result.success and not result.acted and not result.uncertain
    assert "first_name" in result.reason
    before_click.assert_not_called()
    save.first.click.assert_not_called()
    page.wait_for_url.assert_not_called()


def test_confirm_common_screen_click_error_still_succeeds_on_url_change(monkeypatch):
    # #913: click() может упасть при состоявшемся переходе — исход решает
    # wait_for_url, а не сам клик.
    page, save, _locators = _confirm_page(monkeypatch, click_error=True)
    result = common.confirm_common_screen(page, "00001")
    assert result.success and result.acted


def test_confirm_common_screen_no_navigation_is_uncertain(monkeypatch):
    from playwright.sync_api import Error as PlaywrightError

    page, _save, _locators = _confirm_page(monkeypatch, nav_error=PlaywrightError("timeout"))
    result = common.confirm_common_screen(page, "00001")
    assert not result.success and result.acted and result.uncertain
    assert "переход с экрана common не подтверждён" in result.reason


# --- #989: защищённый NEXT-клик + дамп экрана в save_common -------------------


def test_save_common_succeeds_on_url_change(monkeypatch):
    page, save, _locators = _confirm_page(monkeypatch)
    monkeypatch.setattr(common, "dump_page_html", MagicMock())
    result = common.save_common(page, common.CommonValues(), before_click=MagicMock())
    assert result.success and result.acted and not result.uncertain
    save.first.click.assert_called_once_with()
    page.wait_for_url.assert_called_once()
    common.dump_page_html.assert_not_called()


def test_save_common_click_error_still_succeeds_on_url_change(monkeypatch):
    # Паттерн #913: click() может упасть при состоявшемся переходе — исход
    # решает wait_for_url, а не сам клик.
    page, _save, _locators = _confirm_page(monkeypatch, click_error=True)
    result = common.save_common(page, common.CommonValues())
    assert result.success and result.acted


def test_save_common_no_navigation_is_uncertain_and_dumps_screen(monkeypatch):
    from playwright.sync_api import Error as PlaywrightError

    page, _save, _locators = _confirm_page(monkeypatch, nav_error=PlaywrightError("timeout"))
    dump = MagicMock()
    monkeypatch.setattr(common, "dump_page_html", dump)
    result = common.save_common(page, common.CommonValues())
    assert not result.success and result.acted and result.uncertain
    assert "сохранение common не подтверждено" in result.reason
    dump.assert_called_once_with(page, "common_save_failure")


def test_save_common_dump_failure_does_not_mask_uncertain(monkeypatch):
    from playwright.sync_api import Error as PlaywrightError

    page, _save, _locators = _confirm_page(monkeypatch, nav_error=PlaywrightError("timeout"))
    monkeypatch.setattr(
        common, "dump_page_html", MagicMock(side_effect=RuntimeError("dump crashed"))
    )
    result = common.save_common(page, common.CommonValues())
    assert not result.success and result.acted and result.uncertain
    assert "сохранение common не подтверждено" in result.reason
