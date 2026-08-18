"""Smoke checks for dual Claude Code and Codex plugin packaging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_codex_plugin_manifest_exposes_all_skills():
    root = _repo_root()
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())

    assert manifest["name"] == "hhru-cc-plugin"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["capabilities"] == ["Read", "Write"]
    assert sorted(path.parent.name for path in (root / "skills").glob("*/SKILL.md")) == [
        "hhru",
        "hhru-apply",
        "hhru-market",
        "hhru-monitor",
    ]


def test_shared_skills_use_the_installed_hhru_cli():
    root = _repo_root()
    skill_text = "\n".join(path.read_text() for path in (root / "skills").glob("*/SKILL.md"))

    assert "CLAUDE_PLUGIN_ROOT" not in skill_text
    assert "hhru " in skill_text
