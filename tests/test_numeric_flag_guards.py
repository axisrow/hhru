"""#1288: числовые флаги остальных команд за пределами представимости.

Тот же класс падения, что #1279 (stats/funnel): ``--limit`` сверх int64
падал OverflowError при связке в SQLite («Python int too large to convert
to SQLite INTEGER») с traceback вместо [FAIL] + exit 1.
"""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import learn as learn_cmd
from hhru_bot.commands import resume_views as resume_views_cmd
from hhru_bot.commands import robot_queue as robot_queue_cmd
from hhru_bot.commands import uncertain as uncertain_cmd

pytestmark = pytest.mark.integration


def test_learn_limit_beyond_sqlite_int64_exits_with_clear_error(capsys):
    """Гард стоит до open_market: отказ не требует ни конфига, ни market.db."""
    args = argparse.Namespace(config="unused", resume=None, limit=2**63)

    with pytest.raises(SystemExit) as exc:
        learn_cmd.run(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--limit" in err


def test_learn_limit_sqlite_int64_max_boundary_works(capsys, tmp_path, monkeypatch):
    """Верхняя граница int64 (2**63-1) остаётся допустимой."""
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", tmp_path / "market.db")
    args = argparse.Namespace(config="unused", resume=None, limit=2**63 - 1)

    learn_cmd.run(args)

    assert "skill" in capsys.readouterr().out


def test_uncertain_list_limit_beyond_sqlite_int64_exits_with_clear_error(capsys, tmp_path):
    args = argparse.Namespace(history=str(tmp_path / "h.db"), limit=2**63)

    with pytest.raises(SystemExit) as exc:
        uncertain_cmd.list_run(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--limit" in err


def test_uncertain_list_limit_sqlite_int64_max_boundary_works(capsys, tmp_path):
    args = argparse.Namespace(history=str(tmp_path / "h.db"), limit=2**63 - 1)

    uncertain_cmd.list_run(args)

    assert "Очередь uncertain пуста" in capsys.readouterr().out


def test_robot_queue_limit_beyond_sqlite_int64_exits_with_clear_error(capsys, tmp_path):
    args = argparse.Namespace(history=str(tmp_path / "h.db"), limit=2**63)

    with pytest.raises(SystemExit) as exc:
        robot_queue_cmd.run(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--limit" in err


def test_resume_views_limit_beyond_sqlite_int64_exits_with_clear_error(capsys, tmp_path):
    """Отказ до чтения страниц: браузер даже не открывается."""
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        history=str(tmp_path / "h.db"),
        resume=None,
        limit=2**63,
        max_pages=5,
        headless=True,
    )

    with pytest.raises(SystemExit) as exc:
        resume_views_cmd.run(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--limit" in err
