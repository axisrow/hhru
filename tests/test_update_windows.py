"""Windows console-launcher coverage for the unified updater."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.name != "nt", reason="Windows console launcher only")
def test_hhru_exe_reexecs_real_upgrade_before_pip_replaces_launcher(tmp_path: Path):
    """Run a real installed ``hhru.exe update`` against local Git fixtures.

    The test interpreter deliberately installs a non-editable package, so the
    updater reaches its pip-install path.  The fake Codex executable only
    replaces network/plugin discovery; the generated Windows launcher and
    the real update/provenance code run in a child process.
    """
    repository = Path(__file__).parents[1]
    checkout = tmp_path / "checkout"
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout
    checkout.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(checkout, filter="data")

    marketplace_path = checkout / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    plugin = next(item for item in marketplace["plugins"] if item["name"] == "hhru-cc-plugin")
    plugin["source"]["url"] = checkout.as_uri()
    plugin["source"]["ref"] = "main"
    marketplace_path.write_text(json.dumps(marketplace), encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Windows updater test")
    git("add", ".")
    git("commit", "-qm", "fixture")
    git("branch", "-M", "main")
    git("remote", "add", "origin", checkout.as_uri())

    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "sitecustomize.py").write_text(
        "from hhru_bot import update\n"
        "import os\n"
        "update.DEFAULT_SOURCE = os.environ['HHRU_TEST_UPDATE_SOURCE']\n",
        encoding="utf-8",
    )
    codex_script = harness / "fake_codex.py"
    codex_script.write_text(
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['HHRU_TEST_CODEX_LOG']).open('a', encoding='utf-8').write(\n"
        "    ' '.join(args) + '\\n'\n"
        ")\n"
        "root = os.environ['HHRU_TEST_CHECKOUT']\n"
        "if args[:3] == ['plugin', 'marketplace', 'list']:\n"
        "    print('{}')\n"
        "elif args[:3] == ['plugin', 'marketplace', 'add']:\n"
        "    print('{}')\n"
        "elif args[:4] == ['plugin', 'marketplace', 'upgrade', 'hhru']:\n"
        "    print(json.dumps({'upgradedRoots': [root], 'errors': []}))\n"
        "elif args[:2] == ['plugin', 'list']:\n"
        "    print(json.dumps({'installed': []}))\n"
        "elif args[:2] == ['plugin', 'add']:\n"
        "    print(json.dumps({'installedPath': root}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected fake Codex args: {args!r}')\n",
        encoding="utf-8",
    )
    codex = harness / "codex.cmd"
    codex.write_text(f'@echo off\n"{sys.executable}" "{codex_script}" %*\n', encoding="utf-8")

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    launcher = Path(sysconfig.get_path("scripts")) / "hhru.exe"
    assert launcher.is_file()

    environment = os.environ.copy()
    environment.update(
        {
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "HHRU_TEST_CHECKOUT": str(checkout),
            "HHRU_TEST_CODEX_LOG": str(tmp_path / "codex.log"),
            "HHRU_TEST_UPDATE_SOURCE": checkout.as_uri(),
            "HHRU_UPDATE_REEXEC": "",
            # The Windows runner's active code page cannot encode the CLI's
            # Russian status messages; keep the launcher child deterministic.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(harness)
            + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""),
        }
    )
    result = subprocess.run(
        [str(launcher), "update", "--codex", str(codex)],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "[OK] hhru" in result.stdout
    codex_calls = (tmp_path / "codex.log").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("plugin marketplace upgrade hhru") for line in codex_calls)
    assert any(line.startswith("plugin add hhru-cc-plugin@hhru") for line in codex_calls)
