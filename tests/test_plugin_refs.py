"""Regression tests for installable plugin marketplace refs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts import check_plugin_refs  # noqa: E402

pytestmark = pytest.mark.smoke


def _write_marketplace(root: Path, *, url: str, ref: str) -> None:
    path = root / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "hhru-cc-plugin", "source": {"source": "url", "url": url, "ref": ref}}
                ]
            }
        ),
        encoding="utf-8",
    )


def test_check_refs_accepts_a_remote_ref(tmp_path, monkeypatch):
    _write_marketplace(tmp_path, url="https://github.com/axisrow/hhru.git", ref="v0.1.0")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append(command)
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(command, 0, "0123 refs/tags/v0.1.0\n", "")

    monkeypatch.setattr(check_plugin_refs.subprocess, "run", fake_run)

    assert check_plugin_refs.check_refs(tmp_path) == []
    assert calls == [
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--refs",
            "https://github.com/axisrow/hhru.git",
            "refs/tags/v0.1.0",
        ]
    ]


def test_check_refs_rejects_a_missing_remote_ref(tmp_path, monkeypatch):
    _write_marketplace(tmp_path, url="https://github.com/axisrow/hhru.git", ref="v0.1.0")

    def fake_run(command, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(command, 2, "", "fatal: ref not found\n")

    monkeypatch.setattr(check_plugin_refs.subprocess, "run", fake_run)

    errors = check_plugin_refs.check_refs(tmp_path)
    assert len(errors) == 1
    assert "tag 'v0.1.0' does not resolve" in errors[0]
