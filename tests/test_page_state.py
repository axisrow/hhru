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
    assert manifest["permissions"] == []
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
