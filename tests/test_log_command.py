"""Тесты команды log (#58): хвост logs/hhru_bot.log (READ, #21).

Без браузера. Тестируется чистая логика чтения/слежения на tmp-файле:
последние N строк, -n <count>, отсутствие файла -> nonzero exit, -f прерывается
по одному тику polling (мок stop_after). Команда ничего не фильтрует —
редакцией ID занимаются уровни логирования.
"""

from __future__ import annotations

import argparse
import sys
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


def test_tail_lines_tolerates_invalid_utf8(tmp_path):
    """Невалидный UTF-8 не роняет read — errors="replace" подставляет U+FFFD.

    Цикл ревью #61, раунд 3: при truncate-and-regrow-race read мог начаться
    внутри многобайтового символа и упасть UnicodeDecodeError'ом, роняя follow.
    Лог пишется Python-логгером (валидный UTF-8), но errors="replace" даёт
    робастность в диагностическом edge-case.
    """
    path = tmp_path / "hhru_bot.log"
    path.write_bytes(b"valid line\n\xff\xfe invalid bytes\n")
    lines = tail_lines(path, n=50)
    assert len(lines) == 2
    assert lines[0] == "valid line"
    assert "�" in lines[1]  # невалидные байты заменены, не UnicodeDecodeError


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
    def append_before_read(_p, _pos):
        with open(_p, "a", encoding="utf-8") as f:
            f.write("appended\n")

    # stop_after=1 → ровно один polling-цикл, затем loop завершается сам.
    follow(
        path,
        emit,
        sleep_interval=0,
        stop_after=1,
        before_read=append_before_read,
    )
    assert "".join(out) == "appended\n"


def test_follow_initial_lines_then_polling_one_descriptor(tmp_path):
    """initial_lines + polling на одном дескрипторе — race «хвост → follow».

    Цикл ревью #61, находка Codex: раньше run() звал tail_lines (читал до EOF и
    закрывал файл), затем follow переоткрывал и seek'нул к новому EOF — строка,
    дописанная в окно, терялась. Теперь initial_lines печатается из того же
    дескриптора, что и polling, поэтому строка, дописанная перед первым read,
    подхватывается ровно один раз (не теряется, не дублируется).
    """
    path = _log_file(tmp_path, ["a", "b", "c"])
    out: list[str] = []

    def emit(chunk: str) -> None:
        out.append(chunk)

    def append_before_read(_p, _pos):
        with open(_p, "a", encoding="utf-8") as f:
            f.write("d\n")

    follow(
        path,
        emit,
        initial_lines=2,
        sleep_interval=0,
        stop_after=1,
        before_read=append_before_read,
    )
    joined = "".join(out)
    # начальный хвост (последние 2: b, c) + дописанная строка d — без потери.
    assert "b\n" in joined and "c\n" in joined
    assert joined.count("d\n") == 1
    # a в снапшот не попало (за пределами initial_lines=2), в polling тоже нет.
    assert "a\n" not in joined


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
            before_read=raise_interrupt,
        )
    assert exc.value.code == 130


def test_follow_survives_truncation(tmp_path):
    """Лог усечён во время follow — новые записи не теряются (copytruncate).

    Цикл ревью #61, раунд 2, находка Codex: без truncation-detection offset
    оставался за новым EOF и записи пропускались, пока файл не перерастёт
    прежний размер.
    """
    path = _log_file(tmp_path, ["old1", "old2", "old3"])
    out: list[str] = []

    called = {"n": 0}

    def truncate_then_append(_p, _pos):
        called["n"] += 1
        if called["n"] == 1:
            # copytruncate: обрезаем файл, потом пишем свежий контент
            with open(_p, "w", encoding="utf-8") as f:
                f.write("fresh1\nfresh2\n")

    follow(
        path,
        out.append,
        initial_lines=0,
        sleep_interval=0,
        stop_after=1,
        before_read=truncate_then_append,
    )
    joined = "".join(out)
    assert "fresh1" in joined and "fresh2" in joined


# --- realtime flush при pipe -------------------------------------------------


def test_flushing_stdout_write_flushes_after_each_chunk(monkeypatch):
    """emit для follow flush'ит stdout после каждой записи (realtime при pipe).

    Цикл ревью #61, раунд 2, находка Codex: без flush `log -f | grep` висел до
    заполнения block-buffered stdout.
    """
    from hhru_bot.commands.log_cmd import _flushing_stdout_write

    flushed: list[int] = []
    written: list[str] = []

    class _FakeStdout:
        def write(self, s):
            written.append(s)

        def flush(self):
            flushed.append(len(written))

    monkeypatch.setattr(sys, "stdout", _FakeStdout())
    _flushing_stdout_write("a")
    _flushing_stdout_write("b")
    assert written == ["a", "b"]
    assert len(flushed) == 2  # flush после КАЖДОЙ записи


# --- -n валидация ---------------------------------------------------------


def test_register_lines_type_rejects_negative():
    """argparse type=_positive_int: -n -1 -> ArgumentTypeError (exit 2).

    Цикл ревью #61: без валидатора deque(maxlen=-1) ронял команду ValueError'ом,
    а -n 0 молча печатал пустой хвост. Теперь явная понятная ошибка.
    """
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    from hhru_bot.commands.log_cmd import register

    register(sub)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["log", "-n", "-1"])
    assert exc.value.code == 2


def test_register_lines_type_rejects_zero():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    from hhru_bot.commands.log_cmd import register

    register(sub)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["log", "-n", "0"])
    assert exc.value.code == 2


# --- cli.main пропускает setup_logging для log ----------------------------


def test_log_command_does_not_create_log(tmp_path, monkeypatch):
    """cli.main('log ...') НЕ создаёт logs/hhru_bot.log (READ-контракт #21).

    Цикл ревью #61, находка Codex: setup_logging открывал FileHandler на запись
    до run(), из-за чего missing-file-ветка была недостижима и команда «писала»
    локально. Теперь для log setup_logging не вызывается — файл не создаётся,
    и `log` честно рапортует об отсутствии лога.
    """
    monkeypatch.chdir(tmp_path)
    from hhru_bot.cli import main

    # лога нет и быть не должно после запуска
    log_path = tmp_path / "logs" / "hhru_bot.log"
    assert not log_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(["log"])
    assert exc.value.code != 0  # нет файла -> nonzero

    # ключевой ассерт: READ-команда не создала файл/директорию
    assert not log_path.exists()
    assert not (tmp_path / "logs").exists()
