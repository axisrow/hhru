"""Narrow anti-bot challenge detection for apply runs (#344).

This module only detects and reports a challenge.  It never attempts to solve,
submit, or bypass one.  Detection deliberately uses exact URL path segments and
visible, challenge-specific DOM markers: a bare ``captcha`` substring elsewhere
in the page markup or vacancy title is not evidence and must not stop a run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

_CHALLENGE_PATH_SEGMENTS = frozenset({"captcha", "checkpoint", "nocaptcha"})

# Exact, visible challenge controls from the audited references.  Generic
# ``[data-qa*='captcha']`` / ``[class*='captcha']`` selectors are intentionally
# excluded: dormant templates and unrelated vacancy markup could otherwise halt
# a healthy run (the failure mode is still only a hypothesis, #344).
ANTIBOT_MARKER_SELECTORS: tuple[tuple[str, str], ...] = (
    ("recaptcha_iframe", 'iframe[src*="recaptcha" i]'),
    ("hcaptcha_iframe", 'iframe[src*="hcaptcha" i]'),
    ("captcha_iframe", 'iframe[src*="captcha" i]'),
    ("captcha_iframe_title", 'iframe[title*="captcha" i]'),
    ("captcha_data_qa", "[data-qa='captcha']"),
    ("account_captcha_input", "[data-qa='account-captcha-input']"),
    ("account_captcha_picture", "[data-qa='account-captcha-picture']"),
    ("google_recaptcha", ".g-recaptcha"),
    ("hcaptcha", ".h-captcha"),
)


@dataclass(frozen=True)
class AntiBotDetection:
    """Confirmed narrow signal that should terminate the current command."""

    signal: str
    detail: str


class AntiBotChallengeDetected(RuntimeError):
    """Terminal apply-run exception: a human must resolve the challenge."""

    def __init__(self, detection: AntiBotDetection):
        self.detection = detection
        super().__init__(
            "обнаружена анти-бот проверка "
            f"({detection.detail}); решите её вручную и повторите запуск"
        )


def detect_antibot_challenge(
    *, url: str, visible_markers: Iterable[str]
) -> AntiBotDetection | None:
    """Pure decision function over an URL and already-observed visible markers."""

    try:
        path_segments = {segment.casefold() for segment in urlparse(url).path.split("/") if segment}
    except (TypeError, ValueError):
        path_segments = set()
    if challenge_segment := next(
        (segment for segment in _CHALLENGE_PATH_SEGMENTS if segment in path_segments), None
    ):
        return AntiBotDetection("url_path", f"URL содержит /{challenge_segment}")

    observed = set(visible_markers)
    if marker := next(
        (name for name, _selector in ANTIBOT_MARKER_SELECTORS if name in observed), None
    ):
        return AntiBotDetection(marker, f"виден маркер {marker}")
    return None


def detect_antibot_on_page(page: Page) -> AntiBotDetection | None:
    """Read narrow visible signals from a Page, then delegate to the pure detector.

    A selector read error is not itself evidence of a challenge.  The surrounding
    pipeline retains its normal fail-closed handling for broken/closed pages.
    """

    try:
        url = page.url
    except (AttributeError, PlaywrightError):
        url = ""

    visible: list[str] = []
    for name, selector in ANTIBOT_MARKER_SELECTORS:
        try:
            if page.locator(selector).filter(visible=True).count() > 0:
                visible.append(name)
        except (AttributeError, PlaywrightError):
            continue
    return detect_antibot_challenge(url=url, visible_markers=visible)


def raise_for_antibot(page: Page) -> None:
    """Raise the terminal command signal when the narrow detector confirms it."""

    if detection := detect_antibot_on_page(page):
        raise AntiBotChallengeDetected(detection)
