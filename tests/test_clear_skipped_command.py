"""Тесты команды clear-skipped (#87): очистка журнала отсева.

``clear-skipped [--reason <r>] [--dry-run]`` — удаляет записи из таблицы
``skipped``, пишет в БД (WRITE-local). Без браузера, только SQLite.
Регистрируется автоматически через pkgutil (cli.py не трогается).
"""

from __future__ import annotations

import argparse

from hhru_bot.commands import clear_skipped as clear_cmd
from hhru_bot.history import SKIP_REASONS, History


def _args(history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": None,  # clear-skipped не читает config
        "history": str(history_path),
        "reason": None,
        "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed(history: History) -> None:
    history.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    history.record_skip("r1", "v2", SKIP_REASONS.STOPWORD_TITLE)
    history.record_skip("r1", "v3", SKIP_REASONS.STOPWORD_EMPLOYER)
    history.record_skip("r2", "v9", SKIP_REASONS.ALREADY_APPLIED)


def test_clear_all_deletes_everything(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    _seed(h)
    deleted = clear_cmd.run(_args(tmp_path / "h.db"))
    assert deleted == 4
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "4" in out
    assert not h.is_skipped("r1", "v1")


def test_clear_by_reason(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    _seed(h)
    deleted = clear_cmd.run(_args(tmp_path / "h.db", reason=SKIP_REASONS.STOPWORD_TITLE))
    assert deleted == 2  # v1, v2
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "2" in out
    # Остались stopword_employer и already_applied.
    assert h.is_skipped("r1", "v3")
    assert h.is_skipped("r2", "v9")


def test_clear_dry_run_deletes_nothing(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    _seed(h)
    deleted = clear_cmd.run(
        _args(tmp_path / "h.db", reason=SKIP_REASONS.STOPWORD_TITLE, dry_run=True)
    )
    assert deleted == 0
    out = capsys.readouterr().out
    assert "2" in out  # сообщаем сколько БЫЛО бы удалено
    assert "dry-run" in out.lower() or "ничего не удалено" in out.lower()
    # Реально ничего не удалили.
    assert h.is_skipped("r1", "v1")


def test_clear_all_dry_run(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    _seed(h)
    clear_cmd.run(_args(tmp_path / "h.db", dry_run=True))
    # Все 4 на месте.
    assert h.is_skipped("r1", "v1")
    assert h.is_skipped("r2", "v9")


def test_clear_empty_returns_zero(capsys, tmp_path):
    History(tmp_path / "h.db")  # пустая история
    deleted = clear_cmd.run(_args(tmp_path / "h.db"))
    assert deleted == 0
    out = capsys.readouterr().out
    assert "0" in out


def test_clear_unknown_reason_returns_zero(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    _seed(h)
    deleted = clear_cmd.run(_args(tmp_path / "h.db", reason=SKIP_REASONS.HAS_QUESTIONS))
    assert deleted == 0
    # Ничего не задето.
    assert h.is_skipped("r1", "v1")


def test_register_adds_reason_choices():
    import argparse as ap

    sub = ap.ArgumentParser().add_subparsers()
    clear_cmd.register(sub)
    # Извлекаем парсер команды clear-skipped из subparsers.
    action = next(
        a for a in sub.choices["clear-skipped"]._actions if "--reason" in a.option_strings
    )
    # choices = все enum-значения.
    from hhru_bot.history import SKIP_REASON_VALUES

    assert set(action.choices) == set(SKIP_REASON_VALUES)
