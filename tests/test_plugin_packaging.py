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


def test_codex_repo_marketplace_points_to_the_team_plugin():
    root = _repo_root()
    marketplace = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
    plugin = marketplace["plugins"][0]

    assert marketplace["name"] == "hhru"
    assert plugin["name"] == "hhru-cc-plugin"
    assert plugin["source"] == {
        "source": "url",
        "url": "https://github.com/axisrow/hhru.git",
        "ref": "main",
    }
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert plugin["category"] == "Productivity"


def test_shared_skills_use_the_installed_hhru_cli():
    root = _repo_root()
    skill_text = "\n".join(path.read_text() for path in (root / "skills").glob("*/SKILL.md"))

    assert "CLAUDE_PLUGIN_ROOT" not in skill_text
    assert "hhru " in skill_text


def test_all_codex_skills_request_browser_permission_before_first_launch():
    root = _repo_root()
    skills = sorted((root / "skills").glob("*/SKILL.md"))

    for path in skills:
        text = path.read_text()
        assert "sandbox_permissions=require_escalated" in text, path
        assert "перв" in text.casefold(), path
        assert "elevated-разрешение" in text.casefold(), path


def test_hhru_skill_keeps_long_commands_observable_until_exit():
    root = _repo_root()
    text = (root / "skills" / "hhru" / "SKILL.md").read_text()

    assert "run_in_background" in text
    assert "--execution-mode foreground" in text
    assert "--progress-verbosity 1" in text
    assert "--auth-mode anonymous" in text
    assert "--detail-workers 10" in text
    assert "не реже одного раза в 60 секунд" in text
    assert "пока команда не вернёт exit code" in text
    assert "не говори, что процесс" in text
    assert "дождись `[STOP]`" in text
