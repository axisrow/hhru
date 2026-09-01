"""Direct wizard save against the live #911 battle2 DOM fixtures (#913).

Модалка «Уточните специальность» редуцирована из боевых дампов #911
(battle2/clean, 2026-09-01): открытость по пяти признакам, вложенный
``role=treeitem`` — единственный носитель ``aria-selected``, вырождение
неточного поиска в «Другое» (id 40).
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import hhru_bot.resume_position as resume_position
from hhru_bot.create_resume import select_catalog_leaf

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
MODAL_SEARCH_FIXTURE = FIXTURES / "resume_position_profession_modal_search_913.html"
MODAL_OTHER_FIXTURE = FIXTURES / "resume_position_profession_modal_other_913.html"
CHIP_SELECTED_FIXTURE = FIXTURES / "resume_position_wizard_chip_selected_913.html"
HIDDEN_OVERLAY_FIXTURE = FIXTURES / "resume_position_profession_modal_hidden_overlay_913.html"


def _page(fixture: Path):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(fixture.read_text(encoding="utf-8"))
    return playwright, browser, page


@pytest.mark.browser_unit
def test_profession_modal_is_confirmed_only_by_all_five_signals():
    # Открытая модалка battle2/03: overlay ВИДЕН (один), вложенный role=dialog
    # виден, поиск виден, заголовок «Уточните специальность» — тогда и только
    # тогда прямой путь имеет право работать с деревом (#913, правка от #911:
    # count=1 у overlay означает лишь наличие DOM-узла, не открытие).
    playwright, browser, page = _page(MODAL_SEARCH_FIXTURE)
    try:
        overlay = page.locator(resume_position.WIZARD_CATEGORY_MODAL_OVERLAY)
        assert overlay.count() == 1
        assert overlay.first.is_visible()
        assert resume_position.is_profession_modal_confirmed(page) is True
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_outer_tree_row_does_not_carry_aria_selected_only_inner_treeitem_does():
    # Ловушка измерения #911 (5487227929): внешний
    # [data-qa="tree-selector-item tree-selector-item-124"] атрибута
    # aria-selected не имеет; состояние живёт на ВЛОЖЕННОМ div[role=treeitem].
    # Закрепляем структурой, чтобы будущий код не «читал внешний div».
    playwright, browser, page = _page(MODAL_SEARCH_FIXTURE)
    try:
        outer = page.locator("[data-qa='tree-selector-item tree-selector-item-124']")
        assert outer.count() == 1
        assert outer.get_attribute("aria-selected") is None
        inner = outer.locator("[role='treeitem']")
        assert inner.count() == 1
        assert inner.get_attribute("aria-selected") == "false"
        checkbox = page.locator("[data-qa~='tree-selector-input-124']")
        assert checkbox.count() == 1
        assert checkbox.is_checked() is False
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_select_catalog_leaf_clicks_leaf_row_and_waits_for_checked_state():
    # Доказанная механика battle2: клик по СТРОКЕ листа (не по скрытому input),
    # затем poll is_checked(); ожидаемый role_id обязан совпасть до клика.
    playwright, browser, page = _page(MODAL_SEARCH_FIXTURE)
    try:
        reason = select_catalog_leaf(page, "Тестировщик", expected_role_id="124")
        assert reason == ""
        checkbox = page.locator("[data-qa~='tree-selector-input-124']")
        assert checkbox.is_checked() is True
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_select_catalog_leaf_refuses_other_degeneration_before_any_click():
    # Неточная цель вырождается в «Другое» (id 40) — это отказ с перечнем
    # предложенного, а не выбор (#913, правило 1). Никаких кликов по дереву
    # и submit.
    playwright, browser, page = _page(MODAL_OTHER_FIXTURE)
    try:
        reason = select_catalog_leaf(page, "Инженер по тестированию", expected_role_id="124")
        assert "не найдена в каталоге" in reason
        assert "Другое" in reason
        assert page.locator("[data-qa~='tree-selector-input-40']").is_checked() is False
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_select_catalog_leaf_refuses_leaf_found_with_unexpected_role_id():
    # Лист найден по точному тексту, но его id не совпадает с согласованным
    # role_id — остановка до клика: подмена листа молча записала бы чужую роль.
    playwright, browser, page = _page(MODAL_SEARCH_FIXTURE)
    try:
        reason = select_catalog_leaf(page, "Тестировщик", expected_role_id="148")
        assert "role_id" in reason
        assert page.locator("[data-qa~='tree-selector-input-124']").is_checked() is False
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_chip_selected_state_is_not_a_confirmed_modal_and_keeps_next_reachable():
    # Промежуточное состояние battle2/05 (после NEXT/submit, модалка ещё/уже
    # не смонтирована): подтверждённой модалки НЕТ при видимом wizard NEXT.
    # Код без state-machine wait (модалка ИЛИ уход URL) ошибается здесь.
    playwright, browser, page = _page(CHIP_SELECTED_FIXTURE)
    try:
        assert resume_position.is_profession_modal_confirmed(page) is False
        assert page.locator(resume_position.WIZARD_NEXT).is_visible()
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_hidden_overlay_is_not_a_confirmed_modal():
    # Отдельное состояние (наблюдение #911 от 2026-09-01): overlay-узел
    # смонтирован, но скрыт — модалкой НЕ является и кликам не мешает.
    playwright, browser, page = _page(HIDDEN_OVERLAY_FIXTURE)
    try:
        overlay = page.locator(resume_position.WIZARD_CATEGORY_MODAL_OVERLAY)
        assert overlay.count() == 1
        assert overlay.first.is_visible() is False
        assert resume_position.is_profession_modal_confirmed(page) is False
        assert page.locator(resume_position.WIZARD_NEXT).is_visible()
    finally:
        browser.close()
        playwright.stop()
