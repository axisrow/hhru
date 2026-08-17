"""Проверяет шлюз запуска опасных live-тестов."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


SCRIPT = Path(__file__).parents[1] / "scripts" / "live_test.sh"


def _run_script(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    pytest_stub = tmp_path / "pytest"
    pytest_stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" > "$PYTEST_MARKER"\n')
    pytest_stub.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["PYTEST_MARKER"] = str(tmp_path / "pytest-called")
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_dry_run_does_not_invoke_pytest(tmp_path: Path) -> None:
    result = _run_script(tmp_path, "--dry-run")

    assert result.returncode == 0
    assert result.stdout == "[dry-run] канал ASK работает; ничего не запускаю\n"
    assert not (tmp_path / "pytest-called").exists()


def test_without_dry_run_still_invokes_dangerous_marker(tmp_path: Path) -> None:
    result = _run_script(tmp_path, "--some-option")

    assert result.returncode == 0
    assert (tmp_path / "pytest-called").read_text() == "-m live_write_danger --some-option\n"
