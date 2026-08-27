"""Checks for the CLI/plugin lifecycle provenance doctor (#674)."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot.provenance import (
    ComponentIdentity,
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


def test_noneditable_package_inside_checkout_does_not_inherit_checkout_sha(tmp_path: Path):
    root = tmp_path / "project"
    package = root / ".venv" / "lib" / "python3.12" / "site-packages" / "hhru_bot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)

    assert _git_identity("installed CLI", package, require_package_source=True) is None


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


def test_doctor_recovery_action_clears_drift(capsys, monkeypatch):
    from hhru_bot.cli import build_parser
    from hhru_bot.commands import diagnostics
    from hhru_bot.commands import update as update_command

    state = {
        "components": (
            ComponentIdentity("installed CLI", "0.1.0", None, "a" * 40),
            _identity("marketplace snapshot", sha="b" * 40),
            _identity("installed plugin cache", sha="b" * 40),
        )
    }
    monkeypatch.setattr(
        diagnostics,
        "run_doctor",
        lambda **_: compare_identities(state["components"]),
    )

    def unified_update(**_kwargs):
        state["components"] = tuple(
            ComponentIdentity(component.name, component.version, component.release, "c" * 40)
            for component in state["components"]
        )
        return SimpleNamespace(
            release=SimpleNamespace(version="0.1.0", commit="c" * 40),
            cli_source="test-cli",
            plugin_source="test-plugin",
        )

    monkeypatch.setattr(update_command, "update", unified_update)

    assert diagnostics.run_doctor_command(SimpleNamespace(marketplace=None, plugin_cache=None))
    output = capsys.readouterr().out
    fix_line = next(line for line in output.splitlines() if line.startswith("[FIX] "))
    recommended_argv = shlex.split(fix_line.split(": ", 1)[1])

    # Execute the action advertised by doctor through the real CLI parser. This
    # rejects plugin-only commands and verifies that the recommendation reaches
    # the unified update command rather than merely matching a string literal.
    args = build_parser().parse_args(recommended_argv[1:])
    assert args.func is update_command.run
    assert args.func(args) is None
    capsys.readouterr()

    assert not compare_identities(state["components"]).drift
    assert (
        diagnostics.run_doctor_command(SimpleNamespace(marketplace=None, plugin_cache=None))
        is False
    )
    assert "[OK]" in capsys.readouterr().out
