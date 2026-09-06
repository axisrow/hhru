"""Unit tests for the simple common-screen editor."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

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

    # wait_for_url уважает предикат (как реальный API): навигация-ошибка
    # рождается только когда предикат НЕ удовлетворён текущим url (#995:
    # один и тот же page.wait_for_url используют и _open_common_screen,
    # и confirm/save с разными предикатами).
    def _wait_for_url(predicate, *, wait_until=None, timeout=None):
        if not predicate(page.url) and values["nav_error"] is not None:
            raise values["nav_error"]

    page.wait_for_url.side_effect = _wait_for_url

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
    assert page.wait_for_url.call_count == 2  # #995: один — _open_common_screen


def test_confirm_common_screen_refuses_before_click_on_missing_prefill(monkeypatch):
    page, save, _locators = _confirm_page(monkeypatch, name="")
    before_click = MagicMock()
    result = common.confirm_common_screen(page, "00001", before_click=before_click)
    assert not result.success and not result.acted and not result.uncertain
    assert "first_name" in result.reason
    before_click.assert_not_called()
    save.first.click.assert_not_called()
    # #995: единственный вызов — легитимный wait_for_url в _open_common_screen
    assert page.wait_for_url.call_count == 1


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


# --- #991: гидрационный гейт SAVE перед NEXT-кликом ---------------------------


def _hydration(monkeypatch, sequence):
    """Мок wait_for_react_hydration, отдающий значения из sequence по порядку."""
    calls = []

    def fake(page, selector, *, timeout_ms):
        calls.append(timeout_ms)
        return sequence.pop(0) if sequence else False

    monkeypatch.setattr(common, "wait_for_react_hydration", fake)
    return calls


def test_save_common_hydrated_immediately_clicks_once(monkeypatch):
    """Гидрация с первой попытки: клик один, исход не изменился (#990)."""
    page, save, _locators = _confirm_page(monkeypatch)
    calls = _hydration(monkeypatch, [True])
    result = common.save_common(page, common.CommonValues())
    assert result.success and result.acted and not result.uncertain
    assert calls == [common._SAVE_HYDRATION_TIMEOUT_MS]
    save.first.click.assert_called_once_with()


def test_save_common_second_hydration_attempt_clicks(monkeypatch):
    """Первая попытка гидрации не удалась — клик НЕ отправлялся; вторая
    удалась → ровно один клик, success."""
    page, save, _locators = _confirm_page(monkeypatch)
    _hydration(monkeypatch, [False, True])
    result = common.save_common(page, common.CommonValues())
    assert result.success and result.acted
    save.first.click.assert_called_once_with()


def test_save_common_never_hydrated_fails_closed_without_click(monkeypatch):
    """Гидрации нет за обе попытки: клик не отправлялся — честный failed
    (acted=False, не uncertain), дамп страницы, факт в reason."""
    page, save, _locators = _confirm_page(monkeypatch)
    calls = _hydration(monkeypatch, [False, False])
    dump = MagicMock()
    monkeypatch.setattr(common, "dump_page_html", dump)
    result = common.save_common(page, common.CommonValues())
    assert not result.success
    assert result.acted is False and result.uncertain is False
    save.first.click.assert_not_called()
    assert len(calls) == 2
    dump.assert_called_once_with(page, "common_save_failure")
    assert "гидратирован" in result.reason
    assert "клик не отправлялся" in result.reason


def test_save_common_uncertain_reason_carries_hydration_and_click_error(monkeypatch):
    """Гидрация ок, переход не случился: uncertain + в reason и факт гидрации,
    и текст исключения клика (ревью #990: иначе «дошёл ли клик» неизвестно)."""
    from playwright.sync_api import Error as PlaywrightError

    page, save, _locators = _confirm_page(monkeypatch, click_error=True)
    monkeypatch.setattr(
        common,
        "wait_for_react_hydration",
        lambda page, selector, *, timeout_ms: True,
    )
    page.wait_for_url.side_effect = PlaywrightError("Navigation timeout")
    dump = MagicMock()
    monkeypatch.setattr(common, "dump_page_html", dump)
    result = common.save_common(page, common.CommonValues())
    assert not result.success and result.acted and result.uncertain
    assert "гидрация SAVE: ок" in result.reason
    assert "pointer intercepted" in result.reason
    dump.assert_called_once_with(page, "common_save_failure")


def test_save_common_hydration_gate_precedes_before_click(monkeypatch):
    """Гейт-отказ — pre-click состояние (#476, ревью #992): before_click
    (резерв uncertain-маркера) не вызывается вовсе, клик не отправлялся."""
    page, _save, _locators = _confirm_page(monkeypatch)
    _hydration(monkeypatch, [False, False])
    before_click = MagicMock()
    result = common.save_common(page, common.CommonValues(), before_click=before_click)
    assert not result.success and not result.acted and not result.uncertain
    before_click.assert_not_called()


# --- #993: wizard-shape экрана common (draft) --------------------------------


_WIZARD_ACTIVATOR_SELECTOR = "[data-qa='magritte-select-activator']"


class _WizardShapePage:
    """Экран common визарда черновика (live 2026-09-05): работа-книжка —
    magritte-select в контейнере WORK_TICKET_WIZARD (без <label>), поля
    условий работы и город НЕ рендерятся. Точное размещение активатора
    относительно контейнера дампом не зафиксировано (ревью #994):
    placement="inside" мокает активатор внутри контейнера, "sibling" —
    доступным от родителя; каскад common.py обязан находить оба."""

    def __init__(self, placement="inside"):
        from unittest.mock import MagicMock

        self.url = "https://hh.ru/profile/resume/common?resume=00001"
        self.mouse = MagicMock()
        popup = MagicMock()
        popup.get_by_role.return_value.count.return_value = 1
        self._popup = popup
        # Стабильные моки активатора: одни и те же объекты на каждый вызов
        # locator(), чтобы тест ассертил клики по ним же.
        self.wizard_activator = MagicMock()
        self.wizard_activator.count.return_value = 1
        self.wizard_activator.first.inner_text.return_value = "Да"
        self.wizard_activator.first.evaluate.return_value = "DIV"

        def _empty(selector):
            m = MagicMock()
            m.count.return_value = 0
            return m

        def _activator_scope(selector):
            if selector == _WIZARD_ACTIVATOR_SELECTOR:
                return self.wizard_activator
            return _empty(selector)

        container = MagicMock()
        container.count.return_value = 1
        if placement == "inside":
            container.locator.side_effect = _activator_scope
        else:
            parent = MagicMock()
            parent.locator.side_effect = _activator_scope
            container.locator.side_effect = lambda selector: (
                parent if selector == "xpath=.." else _empty(selector)
            )
        self._wizard_container = container

    def locator(self, selector):
        from hhru_bot.selector_groups import account_profile as ap
        from hhru_bot.selector_groups import resume_page

        m = MagicMock()
        if selector == ap.RESUME_COMMON_FORM:
            m.count.return_value = 1
            return m
        if selector in (
            ap.RESUME_COMMON_FIRST_NAME,
            ap.RESUME_COMMON_LAST_NAME,
            ap.RESUME_COMMON_PHONE,
        ):
            m.count.return_value = 1
            m.first.input_value.return_value = "x"
            return m
        if selector == ap.WORK_TICKET_WIZARD:
            return self._wizard_container
        if selector == resume_page.RESUME_POSITION_DROPDOWN:
            return self._popup
        # area и прочие отсутствующие на wizard-shape поля
        m.count.return_value = 0
        return m

    def get_by_label(self, label, *, exact=False):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.count.return_value = 0
        return m

    def get_by_role(self, role, *, name=None, exact=False):
        from unittest.mock import MagicMock

        return MagicMock()


# --- #995: открытие экрана common (published-redirect и гонка монтирования) ---


class _OpenScreenPage:
    """Страница в момент открытия common: URL управляется тестом,
    wait_for_url моделирует редирект hh.ru (live 2026-09-06: у черновика —
    на /profile/resume/common, у опубликованного — на /resume/{hash})."""

    def __init__(self, final_path, form_attached=True):

        self.url = "https://hh.ru/profile/resume?resume=00001"
        self._final_path = final_path
        self._form_attached = form_attached
        self.locators: dict = {}

    def resolve_redirect(self):
        self.url = f"https://hh.ru{self._final_path}"

    def wait_for_url(self, predicate, *, wait_until=None, timeout=None):
        from playwright.sync_api import Error as PlaywrightError

        self.resolve_redirect()
        if not predicate(self.url):
            raise PlaywrightError("Navigation timeout")

    def locator(self, selector):
        from hhru_bot.selector_groups import account_profile as ap

        loc = self.locators.get(selector)
        if loc is None:
            from unittest.mock import MagicMock

            loc = MagicMock()
            loc.count.return_value = 1
            if selector == ap.RESUME_COMMON_FORM:
                loc.first.wait_for.side_effect = (
                    None if self._form_attached else PlaywrightError("not attached")
                )
            loc.first.inner_text.return_value = "x"
            loc.first.input_value.return_value = "x"
            loc.first.is_checked.return_value = True
            self.locators[selector] = loc
        return loc

    def get_by_label(self, label, *, exact=False):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.count.return_value = 0
        return m


def _hydrated(monkeypatch, ok):
    """Гейт гидрации под контролем теста; возвращает список селекторов."""
    calls = []

    def fake_hydration(page, selector, *, timeout_ms):
        calls.append(selector)
        return ok

    monkeypatch.setattr(common, "wait_for_react_hydration", fake_hydration)
    return calls


def test_wizard_work_ticket_falls_back_to_wizard_container(monkeypatch):
    """«Наличие трудовой книжки» без <label>: фолбэк — каскад
    WORK_TICKET_WIZARD (сначала внутри контейнера) → activator →
    гидрационный гейт → magritte-попап → опция «Да» (#993, ревью #994)."""
    from hhru_bot.selector_groups import account_profile as ap

    page = _WizardShapePage()
    hydration_calls = _hydrated(monkeypatch, ok=True)
    common.apply_common(page, common.CommonValues(work_ticket="true"))
    page.wizard_activator.first.click.assert_called_once()
    assert hydration_calls == [ap.WORK_TICKET_WIZARD]


def test_wizard_work_ticket_sibling_placement(monkeypatch):
    """Второй вариант размещения (ревью #994): активатор доступен от родителя
    контейнера — каскад находит его и гейтит гидрацией так же."""
    page = _WizardShapePage(placement="sibling")
    _hydrated(monkeypatch, ok=True)
    common.apply_common(page, common.CommonValues(work_ticket="true"))
    page.wizard_activator.first.click.assert_called_once()


def test_wizard_work_ticket_refuses_unhydrated(monkeypatch):
    """Активатор не гидратирован (#858): клик потерялся бы молча — честный
    pre-click отказ, клика нет (ревью #994)."""
    from hhru_bot.browser import PageStateIndeterminate

    page = _WizardShapePage()
    _hydrated(monkeypatch, ok=False)
    raised = None
    try:
        common.apply_common(page, common.CommonValues(work_ticket="true"))
        raised = None
    except PageStateIndeterminate as exc:
        raised = exc
    assert raised is not None
    assert "не гидратирован" in str(raised)
    page.wizard_activator.first.click.assert_not_called()


def test_wizard_area_refuses_honestly(monkeypatch):
    """Город на wizard-shape не рендерится: --area — внятный отказ, а не
    «не подтверждено однозначно» (#993, боевой прогон RUN db3ae70b)."""
    from hhru_bot.browser import PageStateIndeterminate

    page = _WizardShapePage()
    try:
        common.apply_common(page, common.CommonValues(area="Москва"))
        raised = None
    except PageStateIndeterminate as exc:
        raised = exc
    assert raised is not None
    assert "не рендерится" in str(raised)


def test_wizard_condition_chip_refuses_honestly(monkeypatch):
    """Условия работы (кроме трудовой книжки) на wizard-shape не рендерятся:
    отказ называет экран (#993, RUN 7a795ced — чтение трудовой книжки)."""
    from hhru_bot.browser import PageStateIndeterminate

    page = _WizardShapePage()
    try:
        common.apply_common(page, common.CommonValues(relocation="ready"))
        raised = None
    except PageStateIndeterminate as exc:
        raised = exc
    assert raised is not None
    assert "не рендерится на экране common визарда" in str(raised)


def test_wizard_read_work_ticket_from_container(monkeypatch):
    """read_common на wizard-shape читает трудовую книжку из контейнера."""
    result = common._read_common(_WizardShapePage())
    assert result.work_ticket == "Да"


def test_open_common_published_resume_refuses_honestly(monkeypatch):
    """#995: у опубликованного резюме hh.ru редиректит на /resume/{hash} —
    честный отказ «визанд только у черновиков» + дамп, а не «форма не
    открылась»."""
    page = _OpenScreenPage(f"/resume/{'0' * 32}")
    dump = MagicMock()
    monkeypatch.setattr(common, "goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "require_authenticated_page", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "dump_page_html", dump)
    try:
        common._open_common_screen(page, "0" * 32)
        raised = None
    except RuntimeError as exc:
        raised = exc
    assert raised is not None and "только у черновиков" in str(raised)
    dump.assert_called_once()


def test_open_common_redirect_timeout_dumps(monkeypatch):
    """Редирект не разрешился за бюджет: отказ с URL + дамп (#995)."""
    page = _OpenScreenPage("/profile/resume")
    from playwright.sync_api import Error as PlaywrightError

    page.wait_for_url = lambda *a, **k: (_ for _ in ()).throw(PlaywrightError("timeout"))
    dump = MagicMock()
    monkeypatch.setattr(common, "goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "require_authenticated_page", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "dump_page_html", dump)
    try:
        common._open_common_screen(page, "0" * 32)
        raised = None
    except RuntimeError as exc:
        raised = exc
    assert raised is not None and "редирект" in str(raised)
    dump.assert_called_once()


def test_open_common_draft_waits_for_spa_mount(monkeypatch):
    """Черновик: редирект на /profile/resume/common, экран монтируется позже
    DCL — wait_for(attached) вместо мгновенного count() (#995)."""
    page = _OpenScreenPage(f"/profile/resume/common?resume={'0' * 32}")
    monkeypatch.setattr(common, "goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "require_authenticated_page", lambda *_a, **_k: None)
    editor = common._open_common_screen(page, "0" * 32)
    # attached (монтирование, #995) + visible (финальное ожидание из #869)
    assert [c.kwargs["state"] for c in editor.first.wait_for.call_args_list] == [
        "attached",
        "visible",
    ]


def test_open_common_unmounted_screen_dumps(monkeypatch):
    """Форма не смонтировалась за бюджет: внятный отказ + дамп (#995)."""
    page = _OpenScreenPage(f"/profile/resume/common?resume={'0' * 32}", form_attached=False)
    dump = MagicMock()
    monkeypatch.setattr(common, "goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "require_authenticated_page", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "dump_page_html", dump)
    try:
        common._open_common_screen(page, "0" * 32)
        raised = None
    except RuntimeError as exc:
        raised = exc
    assert raised is not None and "не смонтировался" in str(raised)
    dump.assert_called_once()
