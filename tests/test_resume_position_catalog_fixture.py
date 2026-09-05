"""Characterization of the resume specialization catalog from live #867."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import hhru_bot.resume_position as resume_position

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "resume_position_specializations.html"
WIZARD_CHIPS_FIXTURE = Path(__file__).parent / "fixtures" / "resume_position_wizard_chips.html"


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    return playwright, browser, page


@pytest.mark.browser_unit
def test_wizard_chip_fixture_is_generic_and_does_not_retain_typed_catalog_leaf():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(WIZARD_CHIPS_FIXTURE.read_text(encoding="utf-8"))
    try:
        chips = page.locator(resume_position.WIZARD_POSITION_CHIP_POPULAR)
        assert chips.count() == 36
        assert page.locator(resume_position.WIZARD_POSITION).input_value() == ""
        assert chips.filter(has_text="Программист, разработчик").count() == 0
        assert chips.filter(has_text="Программист").count() == 0
        assert chips.first.get_attribute("value") == "Администратор"
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_resume_catalog_reuses_one_leaf_id_across_categories():
    playwright, browser, page = _page()
    try:
        options = page.locator(resume_position.SPECIALIZATION_OPTION)
        for label, expected_count, expected_id in (
            ("Учитель, преподаватель, педагог", 1, "101"),
            ("Менеджер по продажам, менеджер по работе с клиентами", 4, "202"),
            ("Дизайнер, художник", 2, "303"),
            ("Водитель, экспедитор", 3, "404"),
        ):
            matches = options.filter(has_text=label)
            assert matches.count() == expected_count
            assert {
                item.get_attribute("data-qa").split("tree-selector-child-")[-1]
                for item in (matches.nth(i) for i in range(matches.count()))
            } == {expected_id}

        page.locator(resume_position.SPECIALIZATION_ADD).click()
        resume_position._set_specializations(
            page,
            [
                "Учитель, преподаватель, педагог",
                "Менеджер по продажам, менеджер по работе с клиентами",
                "Дизайнер, художник",
                "Водитель, экспедитор",
            ],
        )
        assert page.locator(resume_position.SPECIALIZATION_MODAL).is_hidden()
        filtered = page.locator(resume_position.SPECIALIZATION_OPTION)
        assert filtered.count() == 3
        assert {item.get_attribute("data-qa") for item in (filtered.nth(i) for i in range(3))} == {
            "tree-selector-item tree-selector-item-404 tree-selector-child-404"
        }
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        # Составное имя листа: «Учитель» — префикс, фильтр НЕ пуст, точного
        # совпадения нет → отказ с требованием точного имени (#954).
        ("Учитель", "результат фильтра непуст"),
        # Совпадений нет вовсе: контейнер дерева пуст — позитивный empty-state
        # живого DOM 2026-09-04 (замер #954).
        ("Несуществующая специализация", "не найдена в дереве резюме"),
    ],
)
@pytest.mark.browser_unit
def test_resume_catalog_rejects_non_leaf_or_missing_specialization(
    monkeypatch, value, expected_error
):
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        page.locator(resume_position.SPECIALIZATION_ADD).click()
        with pytest.raises(RuntimeError, match=expected_error):
            resume_position._set_specializations(page, [value])
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_set_specializations_missing_leaf_refusal_lists_visible_candidates(monkeypatch):
    """#950/#954: отказ при неточном листе называет число отрисованных
    совпадений и требует точное имя из live-каталога, — перезапуск с первого
    раза, а не перебор (контракт сообщения — #964)."""
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        page.locator(resume_position.SPECIALIZATION_ADD).click()
        with pytest.raises(RuntimeError) as exc_info:
            resume_position._set_specializations(page, ["Менеджер"])
        message = str(exc_info.value)
        assert "результат фильтра непуст" in message
        assert "точного листа «Менеджер» среди них нет" in message
        assert "передайте точное имя листа из live-каталога" in message
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_validate_specializations_confirms_exact_leaf_without_submit(monkeypatch):
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        refusals = resume_position.validate_specializations_against_tree(
            page, ["Водитель, экспедитор"]
        )

        assert refusals == []
        # Панель не сабмитится: submit остаётся на месте, выбор не применён.
        assert page.locator(resume_position.SPECIALIZATION_MODAL).is_visible()
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_validate_specializations_refusal_lists_filtered_candidates(monkeypatch):
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        checks = resume_position.validate_specializations_against_tree(
            page, ["Менеджер", "Водитель, экспедитор"]
        )

        assert len(checks) == 1
        # Непустой фильтр без точного листа (#954): боевой --fallback-other
        # здесь НЕ подставит «Другое» — dry-run помечает чек не-eligible.
        assert checks[0].fallback_eligible is False
        assert "Менеджер" in checks[0].message
        assert "результат фильтра непуст" in checks[0].message
        assert "ближайшие доступные листы: " in checks[0].message
        assert "Менеджер по продажам, менеджер по работе с клиентами" in checks[0].message
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_validate_specializations_empty_filter_is_fallback_eligible(monkeypatch):
    """#954: позитивный empty-state (контейнер прикреплён и пуст) —
    единственный случай, в котором боевой --fallback-other подставит
    «Другое»; dry-run помечает такой чек fallback-eligible."""
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        checks = resume_position.validate_specializations_against_tree(page, ["qqqzzz-нет"])

        assert len(checks) == 1
        assert checks[0].fallback_eligible is True
        assert "пустой результат фильтра" in checks[0].message
        assert "qqqzzz-нет" in checks[0].message
    finally:
        browser.close()
        playwright.stop()
