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


def _fake_suite(monkeypatch: pytest.MonkeyPatch, returncode: int) -> None:
    """Подменить сам запуск сюиты, оставив на проверку решение о бюджете.

    Мокается `run_suite`, а не `subprocess.run`: раньше моки сверяли СПИСОК
    аргументов, и это было единственной их проверкой — команда, падавшая при
    реальном запуске, всё равно давала зелёный тест (#440). Исполнимость
    команды теперь держит test_gate_command_runs_without_external_env, а здесь
    остаётся только арифметика бюджета.
    """
    monkeypatch.setattr(
        check_pytest_budget,
        "run_suite",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode),
    )


def test_main_accepts_run_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_suite(monkeypatch, returncode=0)
    clock = iter((10.0, 69.9))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == 0


def test_main_fails_when_suite_exceeds_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_suite(monkeypatch, returncode=0)
    clock = iter((10.0, 70.1))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == 1


def test_main_preserves_pytest_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_suite(monkeypatch, returncode=pytest.ExitCode.TESTS_FAILED)
    clock = iter((10.0, 10.1))
    monkeypatch.setattr(check_pytest_budget.time, "monotonic", lambda: next(clock))

    assert check_pytest_budget.main() == pytest.ExitCode.TESTS_FAILED


def test_gate_command_runs_without_external_env(tmp_path: Path) -> None:
    """Гейт исполним сам по себе, без переменной окружения из ci.yml (#440).

    Мок-тесты выше сверяют только СПИСОК аргументов и потому зелёные даже
    тогда, когда собранная команда при реальном запуске падает: `-p
    xdist.plugin` поверх включённого autoload регистрирует плагин повторно и
    pytest бросает `ValueError: Plugin already registered`. Здесь команда
    исполняется по-настоящему на игрушечной сюите, поэтому тест держит
    инвариант «гейт работает в любом окружении», а не «argv выглядит верно».
    """
    (tmp_path / "test_toy.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    completed = check_pytest_budget.run_suite(cwd=tmp_path)

    assert completed.returncode == 0, completed
