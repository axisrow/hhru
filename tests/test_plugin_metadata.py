"""Regression tests for the generated plugin release metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hhru_bot import __version__

pytestmark = pytest.mark.smoke

MANIFEST_PATHS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path(".agents/plugins/marketplace.json"),
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_plugin_manifests.py"


def _project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def _synchronize(root: Path, *, check: bool = False) -> bool:
    command = [sys.executable, str(SCRIPT), "--root", str(root)]
    if check:
        command.append("--check")
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _copy_metadata_fixture(source: Path, destination: Path) -> None:
    shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")
    for relative in MANIFEST_PATHS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def test_plugin_manifests_are_currently_generated_from_pyproject():
    root = Path(__file__).resolve().parents[1]

    assert _synchronize(root, check=True)
    version = _project_version(root)
    assert __version__ == version
    codex = json.loads((root / ".codex-plugin/plugin.json").read_text())
    claude_plugin = json.loads((root / ".claude-plugin/plugin.json").read_text())
    claude = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    agents = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    assert codex["version"] == version
    assert claude_plugin["version"] == version
    assert claude["metadata"]["version"] == version
    assert claude["plugins"][0]["version"] == version
    assert agents["metadata"]["version"] == version
    assert agents["plugins"][0]["version"] == version


def test_manifest_check_fails_for_intentionally_drifted_version(tmp_path):
    source = Path(__file__).resolve().parents[1]
    _copy_metadata_fixture(source, tmp_path)
    path = tmp_path / ".codex-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "9.9.9"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    assert not _synchronize(tmp_path, check=True)
    assert _synchronize(tmp_path)
    assert json.loads(path.read_text())["version"] == _project_version(tmp_path)


def test_claude_and_codex_marketplaces_are_distinct_supported_schemas():
    root = Path(__file__).resolve().parents[1]
    claude = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    agents = json.loads((root / ".agents/plugins/marketplace.json").read_text())

    assert claude["plugins"][0]["source"] == "./"
    assert agents["plugins"][0]["source"]["source"] == "url"
    assert agents["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
