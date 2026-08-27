"""Windows console-launcher coverage for the unified updater."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.name != "nt", reason="Windows console launcher only")
def test_hhru_exe_reexecs_update_flow_before_help_exits():
    """Exercise the generated launcher, not a mocked pip subprocess."""
    launcher = shutil.which("hhru")
    if launcher is None:
        pytest.skip("installed hhru.exe launcher is unavailable")
    result = subprocess.run(
        [launcher, "update", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Обновить CLI и Codex plugin" in result.stdout
