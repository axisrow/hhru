"""Общий контракт неопределённого состояния браузерных страниц (#143)."""

import pytest

from hhru_bot.apply.questions import QuestionDetection
from hhru_bot.browser import PAGE_STATE, PageStateIndeterminate
from hhru_bot.commands.probe import PageCheck
from hhru_bot.copy_resume import ResumeListIndeterminate
from hhru_bot.responses import NotAuthenticated

pytestmark = pytest.mark.unit


def test_browser_paths_share_indeterminate_exception_base():
    assert issubclass(NotAuthenticated, PageStateIndeterminate)
    assert issubclass(ResumeListIndeterminate, PageStateIndeterminate)


def test_flagged_page_states_use_common_vocabulary():
    assert QuestionDetection.no().page_state == PAGE_STATE["confirmed"]
    assert (
        QuestionDetection.indeterminate_scope("неизвестно").page_state
        == PAGE_STATE["indeterminate"]
    )
    assert PageCheck("x", "https://hh.ru", unreachable=True).page_state == PAGE_STATE["unreachable"]


def test_hhru_live_extension_manifest_and_detector_contract():
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == ["storage"]
    assert manifest["background"]["service_worker"] == "background.js"
    source = (root / "content.js").read_text()
    for selector in (
        '[role="dialog"]',
        '[role="alertdialog"]',
        '[class*="toast"]',
        '[class*="notification"]',
        '[class*="cookie"]',
    ):
        assert selector in source
    assert "MutationObserver" in source
    assert "element.remove" not in source
    assert "click()" not in source


def test_hhru_live_extension_transport_has_empty_allowlist():
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    background = (root / "background.js").read_text()
    assert "new Set()" in content
    assert "name !== 'hhru-agent'" in background
    assert "action_not_allowed" in background


def test_hhru_live_extension_declares_storage_permission():
    """Codex review of PR #644, finding 1: without "storage" permission
    chrome.storage is undefined, so background.js's write and popup.js's
    read both silently no-op and the diagnostics popup never shows anything.
    """
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    manifest = json.loads((root / "manifest.json").read_text())
    assert "storage" in manifest["permissions"]


def test_hhru_live_extension_scans_existing_dom_before_observing():
    """Codex review of PR #644, finding 2: MutationObserver only reports
    future childList mutations. Without an initial scan, an overlay already
    present at document_idle (e.g. a server-rendered cookie banner) is never
    detected.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    observe_index = content.index("observer.observe(")
    scan_index = content.index("document.querySelectorAll(OVERLAY_SELECTORS")
    assert scan_index < observe_index, (
        "initial DOM scan must run before observer.observe() attaches, "
        "otherwise overlays already in the DOM at document_idle are missed"
    )


def test_hhru_live_extension_observes_attribute_visibility_toggles():
    """Codex review of PR #644, finding 3: a childList-only observer misses
    overlays that are already mounted and revealed via a class/style/
    aria-hidden toggle instead of a DOM insertion (common for cookie
    banners and toasts).
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "attributes: true" in content
    assert "attributeFilter" in content


def test_hhru_live_extension_dedupes_reports_by_element():
    """Codex review of PR #644, finding 4: an added subtree runs
    querySelectorAll over every added node, so an element added as a
    descendant of an already-matched ancestor can be reported twice.
    A per-batch WeakSet must gate report() so each element is reported at
    most once per mutation callback invocation.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "WeakSet" in content
