"""#344: narrow anti-bot detection without launching a browser."""

from __future__ import annotations

import pytest

from hhru_bot.apply.antibot import (
    ANTIBOT_MARKER_SELECTORS,
    detect_antibot_challenge,
    detect_antibot_on_page,
)

pytestmark = pytest.mark.unit


class _Locator:
    def __init__(self, *, present: bool, visible: bool):
        self.present = present
        self.visible = visible

    def filter(self, *, visible: bool | None = None):
        if visible is True and not self.visible:
            return _Locator(present=False, visible=False)
        return self

    def count(self) -> int:
        return int(self.present)


class _Page:
    def __init__(self, url: str, markers: dict[str, tuple[bool, bool]] | None = None):
        self.url = url
        self.markers = markers or {}
        self.queried: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.queried.append(selector)
        present, visible = self.markers.get(selector, (False, False))
        return _Locator(present=present, visible=visible)


@pytest.mark.parametrize("segment", ["captcha", "checkpoint", "nocaptcha"])
def test_detects_exact_challenge_path_segment(segment):
    result = detect_antibot_challenge(
        url=f"https://hh.ru/{segment}?backurl=%2Fvacancy%2F1", visible_markers=()
    )

    assert result is not None
    assert result.signal == "url_path"


@pytest.mark.parametrize(
    "url",
    [
        "https://hh.ru/vacancy/1?next=/captcha",
        "https://hh.ru/vacancy/captcha-engineer",
        "https://hh.ru/employer/nocaptcha-labs",
    ],
)
def test_does_not_match_captcha_word_outside_exact_path_segment(url):
    assert detect_antibot_challenge(url=url, visible_markers=()) is None


def test_detects_only_visible_allowlisted_marker():
    marker_name, selector = ANTIBOT_MARKER_SELECTORS[0]
    page = _Page("https://hh.ru/vacancy/1", {selector: (True, True)})

    result = detect_antibot_on_page(page)

    assert result is not None
    assert result.signal == marker_name


def test_hidden_challenge_marker_does_not_halt_run():
    _marker_name, selector = ANTIBOT_MARKER_SELECTORS[0]
    page = _Page("https://hh.ru/vacancy/1", {selector: (True, False)})

    assert detect_antibot_on_page(page) is None


def test_generic_captcha_markup_is_not_queried_or_matched():
    generic = "[data-qa='vacancy-captcha-hint']"
    page = _Page("https://hh.ru/vacancy/1", {generic: (True, True)})

    assert detect_antibot_on_page(page) is None
    assert generic not in page.queried


def test_unknown_observed_marker_is_ignored():
    assert (
        detect_antibot_challenge(
            url="https://hh.ru/vacancy/1", visible_markers={"captcha_word_in_markup"}
        )
        is None
    )
