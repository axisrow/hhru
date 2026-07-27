"""Тесты команды log (#58): хвост logs/hhru_bot.log (READ, #21).

Без браузера. Тестируется чистая логика чтения/слежения на tmp-файле:
последние N строк, -n <count>, отсутствие файла -> nonzero exit, -f прерывается
по одному тику polling (мок stop_after). Команда ничего не фильтрует —
редакцией ID занимаются уровни логирования.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hhru_bot.commands import log_cmd
from hhru_bot.commands.log_cmd import follow, tail_lines


def _log_file(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "hhru_bot.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _args(log_path, **overrides) -> argparse.Namespace:
    base = {
        "log_path": str(log_path),
        "lines": 50,
        "follow": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- tail_lines: последние N строк ----------------------------------------


def test_tail_lines_returns_last_n(tmp_path):
    lines = [f"line-{i}" for i in range(100)]
    path = _log_file(tmp_path, lines)
    assert tail_lines(path, n=10) == [f"line-{i}" for i in range(90, 100)]


def test_tail_lines_default_50(tmp_path):
    lines = [f"line-{i}" for i in range(80)]
    path = _log_file(tmp_path, lines)
    assert tail_lines(path, n=50) == [f"line-{i}" for i in range(30, 80)]


def test_tail_lines_fewer_than_n_returns_all(tmp_path):
    """В файле меньше N строк — отдаём всё, что есть (как tail)."""
    path = _log_file(tmp_path, ["a", "b", "c"])
    assert tail_lines(path, n=50) == ["a", "b", "c"]


def test_tail_lines_handles_trailing_newline(tmp_path):
    """Файл оканчивается пустой строкой (двойной \n) — хвост без артефакта."""
    path = tmp_path / "hhru_bot.log"
    path.write_text("a\nb\n\n", encoding="utf-8")
    assert tail_lines(path, n=50) == ["a", "b"]


def test_tail_lines_empty_file(tmp_path):
    path = tmp_path / "hhru_bot.log"
    path.write_text("", encoding="utf-8")
    assert tail_lines(path, n=50) == []


# --- run: вывод последних строк -------------------------------------------


def test_run_prints_last_lines(capsys, tmp_path):
    path = _log_file(tmp_path, [f"l{i}" for i in range(60)])
    log_cmd.run(_args(path))
    out = capsys.readouterr().out
    assert "l50" in out
    assert "l59" in out
    # по умолчанию 50 строк — начало (l49) присутствует, предшествующее (l9) нет
    assert "l49" in out
    assert "l9" not in out


def test_run_n_count(capsys, tmp_path):
    path = _log_file(tmp_path, [f"l{i}" for i in range(60)])
    log_cmd.run(_args(path, lines=5))
    out = capsys.readouterr().out
    assert "l55" in out and "l59" in out
    assert "l54" not in out


def test_run_no_emoji(capsys, tmp_path):
    """Контракт #21: вывод только текст/ASCII, без эмодзи."""
    path = _log_file(tmp_path, ["hello"])
    log_cmd.run(_args(path))
    out = capsys.readouterr().out
    # ни одного символа вне ASCII (эмодзи — это non-ASCII кодовые точки)
    assert out.isascii(), f"в выводе есть non-ASCII: {out!r}"


def test_run_missing_file_exits_nonzero(capsys, tmp_path):
    path = tmp_path / "does-not-exist.log"
    with pytest.raises(SystemExit) as exc:
        log_cmd.run(_args(path))
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert path.name in err or str(path) in err


# --- follow: один тик polling ---------------------------------------------


def test_follow_prints_appended_lines(tmp_path):
    path = _log_file(tmp_path, ["seed"])
    out: list[str] = []

    def emit(chunk: str) -> None:
        out.append(chunk)

    # следим с позиции EOF; один тик polling, на котором допишем строку.
    def append_then_wait(_p, _pos):
        with open(_p, "a", encoding="utf-8") as f:
            f.write("appended\n")

    # stop_after=1 → ровно один polling-цикл, затем loop завершается сам.
    follow(
        path,
        emit,
        sleep_interval=0,
        stop_after=1,
        before_wait=append_then_wait,
    )
    assert "".join(out) == "appended\n"


def test_follow_keyboard_interrupt_exits_130(tmp_path):
    """Ctrl-C в follow -> exit 130 (как main)."""
    path = _log_file(tmp_path, ["seed"])
    out: list[str] = []

    def raise_interrupt(_p, _pos):
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as exc:
        follow(
            path,
            out.append,
            sleep_interval=0,
            stop_after=5,
            before_wait=raise_interrupt,
        )
    assert exc.value.code == 130
