"""Tests for the CI suite-wide pytest budget guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_pytest_budget.py"
_SPEC = importlib.util.spec_from_file_location("check_pytest_budget", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_pytest_budget = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_pytest_budget)

pytestmark = pytest.mark.unit


def test_main_runs_full_suite_and_accepts_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        check_pytest_budget.subprocess,
        "run",
        lambda command, check: calls.append((command, check)) or SimpleNamespace(returncode=0),
    )
    clock = iter((10.0, 89.9))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == 0
    assert calls == [([check_pytest_budget.sys.executable, "-m", "pytest", "-q"], False)]


def test_main_fails_when_suite_exceeds_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_pytest_budget.subprocess,
        "run",
        lambda _command, check: SimpleNamespace(returncode=0),
    )
    clock = iter((10.0, 100.1))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == 1


def test_main_preserves_pytest_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_pytest_budget.subprocess,
        "run",
        lambda _command, check: SimpleNamespace(returncode=pytest.ExitCode.TESTS_FAILED),
    )
    clock = iter((10.0, 10.1))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == pytest.ExitCode.TESTS_FAILED
