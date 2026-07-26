"""Утилиты для работы с локаторами Playwright в пакете apply.

Владелец: #7 (вынесено сюда, чтобы не плодить хелперы в success.py и не
задевать соседние шаги-владельцы). first_locator — основа fallback-цепочек:
перебирает селекторы по порядку и возвращает первый присутствующий на странице.

Локаторы, которые этот хелпер возвращает, — обычные Playwright Locator;
дальше шаг-владелец сам решает, что с ними делать (count/wait_for/click).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


def first_locator(page: Page, *selectors: str) -> Locator | None:
    """Возвращает первый локатор из selectors, присутствующий на странице.

    «Присутствующий» = locator(selector).count() > 0. Перебор идёт строго
    по порядку аргументов — это и задаёт приоритет fallback-цепочки.
    Если ни один селектор не найден — None.
    """
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator
    return None
