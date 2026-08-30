"""Unit tests for the simple common-screen editor."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hhru_bot.common as common
from hhru_bot.common import CommonValues, apply_common, read_common

pytestmark = pytest.mark.unit


def test_common_values_exposes_only_first_slice():
    values = CommonValues(first_name="Ada", last_name="Lovelace", gender="female")
    assert values.provided() == {
        "firstName": "Ada",
        "lastName": "Lovelace",
        "gender": "female",
    }


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
      <input data-qa="resume-edit-firstName" value="A"/>
      <input data-qa="resume-edit-lastName" value="B"/>
      <input data-qa="resume-edit-birthday" value="2000-01-01"/>
      <select data-qa="resume-edit-gender"><option value="female" selected>F</option></select>
      <input data-qa="resume-edit-phone" value="+7"/>
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
