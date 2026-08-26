"""Checks for the CLI/plugin lifecycle provenance doctor (#674)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot.provenance import (
    ComponentIdentity,
    DoctorResult,
    _git_identity,
    _load_json_text,
    _provenance_values,
    compare_identities,
    plugin_cache_identity,
)

pytestmark = pytest.mark.unit


def _identity(name: str, *, version: str = "0.1.0", sha: str = "a" * 40):
    return ComponentIdentity(name, version, "v0.1.0", sha)


def test_doctor_detects_different_sha_even_when_versions_match():
    result = compare_identities(
        (
            _identity("installed CLI", sha="a" * 40),
            _identity("marketplace snapshot", sha="b" * 40),
            _identity("installed plugin cache", sha="a" * 40),
        )
    )

    assert result.drift is True
    assert any("commit SHA" in reason for reason in result.reasons)


def test_doctor_detects_different_version_and_sha():
    result = compare_identities(
        (
            _identity("installed CLI"),
            _identity("marketplace snapshot", version="0.2.0", sha="b" * 40),
            _identity("installed plugin cache"),
        )
    )

    assert result.drift is True
    assert any("version" in reason for reason in result.reasons)
    assert any("commit SHA" in reason for reason in result.reasons)


def test_editable_checkout_uses_git_identity_not_manifest_version(tmp_path: Path):
    root = tmp_path / "checkout"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "hhru-cc-plugin", "version": "0.1.0"}), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nversion = '0.1.0'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)

    identity = _git_identity("installed CLI", root)

    assert identity.source == "git"
    assert identity.version == "0.1.0"
    assert (
        identity.commit_sha
        == subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    )


def test_plugin_cache_reports_missing_provenance(tmp_path: Path):
    root = tmp_path / "cache" / "0.1.0" / ".codex-plugin"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "hhru-cc-plugin", "version": "0.1.0"}), encoding="utf-8"
    )

    identity = plugin_cache_identity(root.parent)

    assert identity.version == "0.1.0"
    assert identity.commit_sha is None
    assert identity.complete is False


def test_direct_url_vcs_info_provides_commit_sha():
    direct_url = _load_json_text(
        json.dumps(
            {
                "url": "https://github.com/axisrow/hhru",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "main",
                    "commit_id": "d" * 40,
                },
            }
        )
    )

    assert direct_url is not None
    release, sha = _provenance_values(direct_url)
    assert release is None
    assert sha == "d" * 40


def test_manifest_provenance_is_used_when_cache_has_no_git_directory(tmp_path: Path):
    root = tmp_path / "cache" / "0.2.0" / ".codex-plugin"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hhru-cc-plugin",
                "version": "0.2.0",
                "provenance": {"release": "v0.2.0", "commit_sha": "c" * 40},
            }
        ),
        encoding="utf-8",
    )

    identity = plugin_cache_identity(root.parent)

    assert identity.complete is True
    assert identity.release == "v0.2.0"
    assert identity.commit_sha == "c" * 40


def test_doctor_prints_one_recovery_command(capsys, monkeypatch):
    from hhru_bot.commands import diagnostics

    components = (_identity("installed CLI"), _identity("marketplace snapshot", sha="b" * 40))
    monkeypatch.setattr(
        diagnostics,
        "run_doctor",
        lambda **_: DoctorResult(components, True, ("different commit SHA",)),
    )

    assert (
        diagnostics.run_doctor_command(SimpleNamespace(marketplace=None, plugin_cache=None)) is True
    )
    output = capsys.readouterr().out
    assert output.count("codex plugin marketplace upgrade hhru --json") == 1
