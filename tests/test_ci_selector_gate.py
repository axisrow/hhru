from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT_RELATIVE_PATH = Path("scripts/selector_contracts.py")

pytestmark = pytest.mark.unit


def _copy_contract_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "repository"
    for relative_path in ("scripts", "selectors", "src/hhru_bot"):
        source = REPOSITORY_ROOT / relative_path
        shutil.copytree(source, fixture / relative_path)
    return fixture


def _run_check(fixture: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_RELATIVE_PATH), "check"],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )


def test_selector_contract_check_passes_on_clean_repository(tmp_path: Path):
    result = _run_check(_copy_contract_fixture(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "selector contracts OK" in result.stdout


def _replace_once(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert content.count(old) == 1
    path.write_text(content.replace(old, new), encoding="utf-8")


def _drift_generated(fixture: Path) -> None:
    _replace_once(
        fixture / "src/hhru_bot/selector_groups/_generated.py",
        '"account_profile.ACCOUNT_PROFILE_CITY": "[data-qa=\'profile-common-card-city\']"',
        '"account_profile.ACCOUNT_PROFILE_CITY": "[data-qa=\'synthetic-drift\']"',
    )


def _drift_unmanaged_literal(fixture: Path) -> None:
    path = fixture / "src/hhru_bot/selector_groups/resume_page.py"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nSYNTHETIC_DRIFT = \"[data-qa='synthetic-drift']\"\n")


def _drift_matrix(fixture: Path) -> None:
    path = fixture / "selectors/reference-matrix.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nsynthetic drift\n")


@pytest.mark.parametrize(
    ("drift_name", "mutate", "expected_error"),
    [
        (
            "generated runtime",
            _drift_generated,
            "generated runtime is stale",
        ),
        (
            "unmanaged literal",
            _drift_unmanaged_literal,
            "unmanaged selector",
        ),
        (
            "reference matrix",
            _drift_matrix,
            "reference matrix is stale",
        ),
    ],
)
def test_selector_contract_check_rejects_drift(
    tmp_path: Path,
    drift_name: str,
    mutate: Callable[[Path], None],
    expected_error: str,
):
    fixture = _copy_contract_fixture(tmp_path)
    mutate(fixture)

    result = _run_check(fixture)

    assert result.returncode == 1, f"{drift_name}: {result.stdout}{result.stderr}"
    assert expected_error in result.stderr
