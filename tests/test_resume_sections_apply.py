"""Browser-level тесты _apply_rows/apply_plan (#352 cycle-review round 1, codex).

Стаб Page/Locator моделирует ровно то, что использует _apply_rows: click(),
locator(...).wait_for(state="visible"), .count(). ``ready_ok=False`` имитирует
таймаут гидратации (PlaywrightTimeoutError из wait_for) ПОСЛЕ того, как
предыдущая строка уже была сохранена — это регрессионный сценарий для находки
codex: до фикса такое исключение вылетало наружу из apply_plan необработанным,
теряя факт частичного сохранения; после фикса — заносится в errors как
конкретная строка, и обработка блока останавливается (fail-closed), не давая
исключению выйти за пределы apply_plan.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.resume_sections import Attestation, _apply_rows

pytestmark = pytest.mark.unit


class FakeSaveButton:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1

    def click(self):
        self._page.saved_rows.append(self._page.current_index)


class FakeReadyLocator:
    def __init__(self, page, ready: bool):
        self._page = page
        self._ready = ready

    def wait_for(self, *, state="visible", timeout=None):  # noqa: ARG002
        if not self._ready:
            raise PlaywrightTimeoutError("гидратация не завершилась вовремя")


class FakeTrigger:
    def __init__(self, page, count):
        self._page = page
        self._count = count
        self._calls = 0

    def count(self):
        # nth-й вызов count() (0-indexed) соответствует итерации цикла с тем же
        # индексом, т.к. _apply_rows зовёт trigger.count() ровно раз за строку.
        call_index = self._calls
        self._calls += 1
        if call_index in self._page._count_fails:
            raise PlaywrightTimeoutError("count() недоступен")
        return self._count

    def nth(self, index):
        return FakeTriggerRow(self._page, index)


class FakeTriggerRow:
    def __init__(self, page, index):
        self._page = page
        self._index = index

    def click(self):
        if self._index in self._page._click_fails:
            raise PlaywrightTimeoutError("триггер не кликается")
        self._page.current_index = self._index


class FakePage:
    """Строка ``ready_by_index[i] = False`` имитирует таймаут гидратации на
    строке i (после того как строки < i уже были кликнуты save). ``click_fails``
    имитирует таймаут САМОГО клика по триггеру строки i (codex, cycle 2:
    trigger.nth(index).click() изначально был вне try/except)."""

    def __init__(
        self,
        *,
        trigger_count: int,
        ready_by_index: dict[int, bool] | None = None,
        click_fails: set[int] | None = None,
        count_fails: set[int] | None = None,
    ):
        self._trigger_count = trigger_count
        self._ready_by_index = ready_by_index or {}
        self._click_fails = click_fails or set()
        self._count_fails = count_fails or set()
        self.saved_rows: list[int] = []
        self.current_index = -1

    def locator(self, selector: str):
        if selector.startswith("[data-qa^="):
            return FakeTrigger(self, self._trigger_count)
        if selector == "[data-qa='profile-layout-save-button']":
            return FakeSaveButton(self)
        # ready_selector для attestations/recommendations
        ready = self._ready_by_index.get(self.current_index, True)
        return FakeReadyLocator(self, ready)


def _fill_row(page, item):  # noqa: ARG001
    return page.locator("[data-qa='profile-layout-save-button']")


def test_hydration_timeout_after_prior_save_is_reported_not_raised():
    """Codex-находка: таймаут wait_for на строке 1 ПОСЛЕ успешного save строки 0
    должен вернуться как элемент errors, а не всплыть исключением из _apply_rows."""
    page = FakePage(trigger_count=2, ready_by_index={1: False})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    # Строка 0 успела сохраниться до таймаута на строке 1.
    assert page.saved_rows == [0]
    # Ошибка сообщает именно про строку 1, а не тонет молча и не падает исключением.
    assert len(errors) == 1
    assert "строка 1" in errors[0]
    assert "attestations" in errors[0]


def test_trigger_click_failure_after_prior_save_is_reported_not_raised():
    """Codex-находка cycle 2: trigger.nth(index).click() строки 1 падает ПОСЛЕ
    успешного save строки 0 — должно вернуться как элемент errors, а не всплыть
    исключением из _apply_rows (клик был вне try/except до этого фикса)."""
    page = FakePage(trigger_count=2, click_fails={1})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert page.saved_rows == [0]
    assert len(errors) == 1
    assert "строка 1" in errors[0]


def test_trigger_count_failure_after_prior_save_is_reported_not_raised():
    """cycle 3 (advisor review): trigger.count() строки 1 падает ПОСЛЕ успешного
    save строки 0 — тот же класс необработанного исключения; count() тоже должен
    быть внутри try/except, не только click()/wait_for()."""
    page = FakePage(trigger_count=2, count_fails={1})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert page.saved_rows == [0]
    assert len(errors) == 1
    assert "строка 1" in errors[0]


def test_all_rows_hydrate_and_save_without_errors():
    page = FakePage(trigger_count=2)
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert errors == []
    assert page.saved_rows == [0, 1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
