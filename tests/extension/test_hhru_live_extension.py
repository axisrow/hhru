import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2] / "extensions" / "hhru-live"


def test_mv3_manifest_has_minimal_hhru_permissions():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == []
    assert all(host.startswith("https://") for host in manifest["host_permissions"])
    assert manifest["background"]["service_worker"] == "background.js"


def test_detector_covers_multiple_popup_shapes_without_close_actions():
    source = (ROOT / "content.js").read_text()
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


def test_agent_transport_has_explicit_empty_allowlist():
    content = (ROOT / "content.js").read_text()
    background = (ROOT / "background.js").read_text()
    assert "new Set()" in content
    assert "name !== 'hhru-agent'" in background
    assert "action_not_allowed" in background
