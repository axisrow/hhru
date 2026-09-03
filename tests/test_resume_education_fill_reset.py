"""#956: Magritte controlled inputs reset asynchronously after fill().

Live failure 2026-09-03 (education editor on a fresh draft): fill() +
immediate input_value() both succeed, but a later async React re-render
remounts the field with the React state still empty, so Save submits an
empty form (validation "Пожалуйста, укажите" on the save-failure dump).
_fill_and_verify must survive the reset window: after the value matches,
it must wait past the async re-render and re-check before reporting
success.
"""

from __future__ import annotations

import pytest

from hhru_bot.resume_education import _fill_and_verify, _pre_save_stable

pytestmark = pytest.mark.unit


class ResettingLocator:
    """Answers like the live combobox: first async tick clears the value."""

    def __init__(self, page):
        self._page = page
        self._value = ""
        self._ticks = 0

    def fill(self, value):
        self._value = value

    def input_value(self):
        return self._value

    def _tick(self):
        # The async re-render lands once: whatever DOM value fill() wrote
        # is replaced by the (empty) React state.
        self._ticks += 1
        if self._ticks == 1:
            self._value = ""


class TickPage:
    def __init__(self):
        self.ticks = 0

    def wait_for_timeout(self, timeout):  # noqa: ARG002
        self.ticks += 1
        self._locator._tick()

    def attach(self, locator):
        self._locator = locator


def test_fill_and_verify_survives_async_reset():
    page = TickPage()
    locator = ResettingLocator(page)
    page.attach(locator)
    assert _fill_and_verify(page, locator, "Медицинский университет") is True
    assert locator.input_value() == "Медицинский университет"
    # Old code returned True after the immediate match without ever waiting,
    # so the async reset landed AFTER verification and Save submitted empty.
    assert page.ticks >= 1


def test_pre_save_stable_survives_reset_after_last_fill(monkeypatch):
    """The final guard must cover the reset window after the last field fill."""
    page = TickPage()
    locator = ResettingLocator(page)
    page.attach(locator)
    monkeypatch.setattr(
        "hhru_bot.resume_education._field_locator",
        lambda page, name, **kwargs: locator,  # noqa: ARG005
    )

    assert (
        _pre_save_stable(
            page,
            [("institution", "Медицинский университет")],
            additional=False,
            trigger_shape=False,
        )
        is True
    )
    assert locator.input_value() == "Медицинский университет"
    assert page.ticks >= 1
