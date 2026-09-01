"""Browser_unit-тесты адресации radio-режимов видимости на живом DOM (#901).

Фикстура ``resume_visibility_cards_901.html`` — реальный read-only дамп экрана
``/resume/edit/{resume_id}/visibility`` (2026-09-01, кликов не было): каждая
карточка ``<label data-qa="resume-visibility-card-access-type-*">`` содержит
ДВА ``input[type=radio]`` — внешний (прямой дочерний label'а) и внутренний
Magritte (вложен в ``span[data-qa='radio-container']``). Проверяемая механика:
клик по карточке-``<label>`` активирует именно внешний radio (первый labelable
потомок внешнего label — нативное поведение, подтверждено на этой фикстуре);
внутренний Magritte синхронизирует React уже на живом hh.ru, поэтому код
читает/проверяет только внешний. В голом Chromium фикстуры внутренний остаётся
unchecked после клика — и не должен читаться кодом.
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import hhru_bot.resume_visibility as rv
from hhru_bot.selector_groups.resume_visibility import (
    RESUME_VISIBILITY_MODE_LINK_ONLY,
    RESUME_VISIBILITY_MODE_NO_ONE,
)

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "resume_visibility_cards_901.html"


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    return playwright, browser, page


@pytest.mark.browser_unit
def test_visibility_card_contains_two_nested_radios_outer_is_direct_child():
    # Структурный факт живого дампа #901: descendant-поиск по карточке находит
    # ДВА radio (внешний + внутренний Magritte) — старая строгая проверка
    # «ровно один input[type='radio'] внутри карточки» fail-closed ломалась
    # именно на этом. Внешний отличим как ПРЯМОЙ дочерний (":scope >").
    playwright, browser, page = _page()
    try:
        card = page.locator(RESUME_VISIBILITY_MODE_NO_ONE)
        assert card.count() == 1
        assert card.locator("input[type='radio']").count() == 2
        assert card.locator(":scope > input[type='radio']").count() == 1
        inner = card.locator("span[data-qa='radio-container'] input[type='radio']")
        assert inner.count() == 1
        assert inner.get_attribute("readonly") is not None
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_click_mode_succeeds_on_two_nested_radio_dom():
    # Главный регресс #901: _click_mode обязан проходить на карточке с двумя
    # вложенными radio — клик по label-карточке активирует внешний radio, и
    # пост-кликовая проверка читает его же (":scope >"), а не descendant-поиск,
    # который находит и внутренний Magritte input.
    playwright, browser, page = _page()
    try:
        reason = rv._click_mode(page, "no-one")
        assert reason == ""
        outer = page.locator(RESUME_VISIBILITY_MODE_NO_ONE).locator(":scope > input[type='radio']")
        assert outer.first.is_checked() is True
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_read_active_mode_returns_checked_outer_radio_mode():
    # Позитивный маркер результата (#901 п.3): активный режим читается по
    # checked внешнего radio карточек. В дампе активен direct → "link-only".
    playwright, browser, page = _page()
    try:
        assert rv.read_active_mode(page) == "link-only"
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_read_active_mode_none_when_no_card_checked():
    # Fail-closed: ни один внешний radio не checked (странная страница/дрейф
    # DOM) — активный режим не определён, а не «первый попавшийся».
    playwright, browser, page = _page()
    try:
        outer = page.locator(RESUME_VISIBILITY_MODE_LINK_ONLY).locator(
            ":scope > input[type='radio']"
        )
        assert outer.first.is_checked() is True
        outer.first.evaluate("el => el.checked = false")
        assert rv.read_active_mode(page) is None
    finally:
        browser.close()
        playwright.stop()


@pytest.mark.browser_unit
def test_read_active_mode_none_when_two_cards_checked():
    # Fail-closed «ровно один»: у внешних radio пустой name — браузер НЕ
    # снимает соседей при нативном клике, эксклюзивность держит React. В голом
    # Chromium фикстуры клик по no-one оставляет checked и на прежнем direct:
    # ровно тот DOM-миг, который живой сайт переживает между кликом и
    # React-синхронизацией. Два checked = режим не определён, а не «первый
    # попавшийся» (порядок итерации _MODE_SELECTORS отдал бы everyone).
    playwright, browser, page = _page()
    try:
        assert rv._click_mode(page, "no-one") == ""
        no_one = page.locator(RESUME_VISIBILITY_MODE_NO_ONE).locator(":scope > input[type='radio']")
        link_only = page.locator(RESUME_VISIBILITY_MODE_LINK_ONLY).locator(
            ":scope > input[type='radio']"
        )
        assert no_one.first.is_checked() is True
        assert link_only.first.is_checked() is True  # браузер не снял соседа
        assert rv.read_active_mode(page) is None
    finally:
        browser.close()
        playwright.stop()
