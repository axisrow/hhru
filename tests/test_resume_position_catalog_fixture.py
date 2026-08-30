"""Characterization of the resume specialization catalog from live #867."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import hhru_bot.resume_position as resume_position

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "resume_position_specializations.html"


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    return playwright, browser, page


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
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.parametrize("value", ["Учитель", "Несуществующая специализация"])
@pytest.mark.browser_unit
def test_resume_catalog_rejects_non_leaf_or_missing_specialization(monkeypatch, value):
    monkeypatch.setattr(resume_position, "_CONTROL_WAIT_TIMEOUT_MS", 50)
    playwright, browser, page = _page()
    try:
        page.locator(resume_position.SPECIALIZATION_ADD).click()
        with pytest.raises(RuntimeError, match="специализация не найдена в дереве резюме"):
            resume_position._set_specializations(page, [value])
    finally:
        browser.close()
        playwright.stop()
