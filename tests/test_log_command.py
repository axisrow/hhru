"""Тесты команды log (#58): хвост data/logs/hhru_bot.log (READ, #21).

Без браузера. Тестируется чистая логика чтения/слежения на tmp-файле:
последние N строк, -n <count>, отсутствие файла -> nonzero exit, -f прерывается
по одному тику polling (мок stop_after). Команда ничего не фильтрует —
редакцией ID занимаются уровни логирования.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pytest

from hhru_bot import logging_setup
from hhru_bot.commands import log_cmd
from hhru_bot.commands.log_cmd import follow, prune_candidates, tail_lines

pytestmark = pytest.mark.integration


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


def test_follow_switches_to_active_file_after_rename_rotation(tmp_path):
    """Rename-based rotation does not leave ``log -f`` on the old inode."""
    path = _log_file(tmp_path, ["old"])
    out: list[str] = []
    called = {"n": 0}

    def rotate_before_read(_p, _pos):
        called["n"] += 1
        if called["n"] == 1:
            path.rename(tmp_path / "hhru_bot.log.1")
            path.write_text("fresh\n", encoding="utf-8")

    follow(
        path,
        out.append,
        initial_lines=0,
        sleep_interval=0,
        stop_after=1,
        before_read=rotate_before_read,
    )

    assert "fresh\n" in out
    assert (tmp_path / "hhru_bot.log.1").read_text(encoding="utf-8") == "old\n"


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
    """cli.main('log ...') НЕ создаёт data/logs/hhru_bot.log (READ-контракт #21).

    Цикл ревью #61, находка Codex: setup_logging открывал FileHandler на запись
    до run(), из-за чего missing-file-ветка была недостижима и команда «писала»
    локально. Теперь для log setup_logging не вызывается — файл не создаётся,
    и `log` честно рапортует об отсутствии лога.

    Изоляция от реального лога (#129): `LOG_DIR`/`DEFAULT_LOG_PATH` абсолютны и
    вычисляются НА ИМПОРТЕ модуля (`Path.cwd() / "data" / "logs"`), поэтому одного
    `chdir(tmp_path)` мало — на машине разработчика, где `data/logs/hhru_bot.log`
    существует, `main(["log"])` читал настоящий лог, печатал его хвост и не
    бросал SystemExit. Подменяем оба пути на tmp_path: `DEFAULT_LOG_PATH` —
    его `register()` кладёт в `set_defaults(log_path=...)` при каждом
    `build_parser()`, то есть внутри `main`; `logging_setup.LOG_DIR` — на случай,
    если setup_logging всё-таки вызовется (регрессия READ-контракта: тогда
    файл создастся в tmp_path и ассерты это увидят, а не молча промахнутся
    мимо реального каталога). Тест по-прежнему проверяет поведение ДЕФОЛТНОГО
    пути — `--log-path` не передаётся.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(log_cmd, "DEFAULT_LOG_PATH", tmp_path / "logs" / "hhru_bot.log")
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
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


# --- log --prune (#717): чистка дампов probe/apply ---------------------------
#
# ЖЁСТКАЯ граница: все prune-тесты работают ТОЛЬКО на tmp-каталоге
# (_prune_args(log_dir=tmp_path)). Реальный data/logs никогда не
# затрагивается — ни один тест не читает и не пишет LOG_DIR.


def _make_dump(path: Path, size: int = 10, mtime: float | None = None) -> Path:
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _prune_args(log_dir, **overrides) -> argparse.Namespace:
    base = {
        "log_path": str(log_dir / "hhru_bot.log"),
        "log_dir": str(log_dir),
        "lines": 50,
        "follow": False,
        "prune": True,
        "older_than": 14,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- prune_candidates: отбор -----------------------------------------------


def test_prune_candidates_selects_old_dumps_only(tmp_path):
    now = 1_700_000_000.0
    old = now - 15 * 86400
    _make_dump(tmp_path / "probe_a.html", mtime=old)
    _make_dump(tmp_path / "probe_a.png", mtime=old)
    _make_dump(tmp_path / "probe_fresh.html", mtime=now - 100)
    assert prune_candidates(tmp_path, 14, now=now) == [
        tmp_path / "probe_a.html",
        tmp_path / "probe_a.png",
    ]


def test_prune_candidates_ignores_logs_and_other_extensions(tmp_path):
    """Логи (hhru_bot.log/scheduled.log) и чужие расширения не кандидаты."""
    now = 1_700_000_000.0
    old = now - 100 * 86400
    _make_dump(tmp_path / "hhru_bot.log", mtime=old)
    _make_dump(tmp_path / "scheduled.log", mtime=old)
    _make_dump(tmp_path / "notes.txt", mtime=old)
    _make_dump(tmp_path / "dump", mtime=old)  # без расширения
    assert prune_candidates(tmp_path, 14, now=now) == []


def test_prune_candidates_not_recursive(tmp_path):
    """Скан не рекурсивен: *.html во вложенном каталоге не трогается."""
    now = 1_700_000_000.0
    nested = tmp_path / "nested"
    nested.mkdir()
    _make_dump(nested / "probe_a.html", mtime=now - 100 * 86400)
    assert prune_candidates(tmp_path, 14, now=now) == []


def test_prune_candidates_age_boundary_is_exclusive(tmp_path):
    """Ровно на пороге (mtime == cutoff) файл НЕ кандидат: «старше N дней»."""
    now = 1_700_000_000.0
    cutoff = now - 14 * 86400
    at_boundary = _make_dump(tmp_path / "at.html", mtime=cutoff)
    just_before = _make_dump(tmp_path / "before.html", mtime=cutoff - 1)
    assert prune_candidates(tmp_path, 14, now=now) == [just_before]
    assert at_boundary not in prune_candidates(tmp_path, 14, now=now)


def test_prune_candidates_older_than_zero_matches_everything(tmp_path):
    now = 1_700_000_000.0
    dump = _make_dump(tmp_path / "probe_a.html", mtime=now - 1)
    assert prune_candidates(tmp_path, 0, now=now) == [dump]


def test_prune_candidates_empty_dir(tmp_path):
    assert prune_candidates(tmp_path, 14, now=1_700_000_000.0) == []


# --- format_size ------------------------------------------------------------


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0 Б"),
        (512, "512 Б"),
        (1024, "1.0 КиБ"),
        (2 * 1024 * 1024, "2.0 МиБ"),
        (253 * 1024 * 1024, "253.0 МиБ"),
    ],
)
def test_format_size(num_bytes, expected):
    assert log_cmd.format_size(num_bytes) == expected


# --- run(): поведение команды ----------------------------------------------


def test_run_prune_dry_run_by_default_deletes_nothing(tmp_path, capsys):
    """Без --yes и TTY (pytest stdin не tty) — только план, файлы на месте."""
    # run() берёт now из time.time() — mtimes задаём относительно реального
    # времени, а не фиксированной эпохи.
    old = _make_dump(tmp_path / "probe_a.html", size=2048, mtime=time.time() - 100 * 86400)
    log = _make_dump(tmp_path / "hhru_bot.log", mtime=time.time() - 100 * 86400)
    log_cmd.run(_prune_args(tmp_path))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "probe_a.html" in out
    assert old.exists()
    assert log.exists()


def test_run_prune_yes_deletes_only_candidates(tmp_path, capsys):
    now = time.time()
    old_html = _make_dump(tmp_path / "probe_a.html", mtime=now - 100 * 86400)
    old_png = _make_dump(tmp_path / "apply_b.png", mtime=now - 100 * 86400)
    fresh_png = _make_dump(tmp_path / "probe_fresh.png", mtime=now - 10)
    log = _make_dump(tmp_path / "hhru_bot.log", mtime=now - 100 * 86400)
    scheduled = _make_dump(tmp_path / "scheduled.log", mtime=now - 100 * 86400)

    log_cmd.run(_prune_args(tmp_path, yes=True))

    out = capsys.readouterr().out
    assert "[OK] Удалено 2 файл(ов)" in out
    assert not old_html.exists()
    assert not old_png.exists()
    for survivor in (fresh_png, log, scheduled):
        assert survivor.exists()


def test_run_prune_no_candidates_reports_ok(tmp_path, capsys):
    _make_dump(tmp_path / "probe_fresh.html", mtime=time.time() - 10)
    log_cmd.run(_prune_args(tmp_path))
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "чистить нечего" in out
    assert (tmp_path / "probe_fresh.html").exists()


def test_run_prune_rejects_follow(tmp_path, capsys):
    """--prune с -f — явный отказ, ничего не удалено."""
    old = _make_dump(tmp_path / "probe_a.html", mtime=time.time() - 100 * 86400)
    with pytest.raises(SystemExit) as exc:
        log_cmd.run(_prune_args(tmp_path, follow=True))
    assert exc.value.code != 0
    assert "[FAIL]" in capsys.readouterr().err
    assert old.exists()


def test_prune_candidates_missing_dir_is_empty(tmp_path):
    """data/logs ещё не существует (свежая установка) — не candidates, не traceback."""
    assert prune_candidates(tmp_path / "logs", 14, now=1_700_000_000.0) == []


def test_run_prune_missing_dir_reports_ok(tmp_path, capsys):
    log_cmd.run(_prune_args(tmp_path / "logs"))
    out = capsys.readouterr().out
    assert "[OK]" in out and "чистить нечего" in out


def test_register_log_dir_follows_isolated_log_dir(tmp_path, monkeypatch):
    """CLI-дефолт log_dir читает logging_setup.LOG_DIR на момент build_parser.

    Страж изоляции #131: _isolate_log_dir патчит logging_setup.LOG_DIR;
    register() обязан взять подменённое значение, иначе `log --prune` из-под
    тестов увидел бы реальный data/logs.
    """
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "isolated")
    from hhru_bot.cli import build_parser

    args = build_parser().parse_args(["log"])
    assert args.log_dir == str(tmp_path / "isolated")


def test_prune_candidates_tolerates_vanishing_file(tmp_path, monkeypatch):
    """Файл, исчезнувший между iterdir и stat, не роняет отбор (гонка как у unlink).

    Ревью PR #923: entry.stat() без guard'а дал бы traceback вместо плана.
    Симуляция: Path.stat бросает FileNotFoundError ровно для одного файла.
    """
    now = time.time()
    gone = _make_dump(tmp_path / "probe_gone.html", mtime=now - 100 * 86400)
    stays = _make_dump(tmp_path / "probe_stays.html", mtime=now - 100 * 86400)
    real_stat = Path.stat

    def racing_stat(self, *a, **kw):
        if self == gone:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", racing_stat)
    assert prune_candidates(tmp_path, 14, now=now) == [stays]


def test_print_prune_plan_tolerates_vanishing_file(tmp_path, capsys, monkeypatch):
    """Файл, исчезнувший между отбором и печатью плана, пропускается без падения."""
    now = time.time()
    gone = _make_dump(tmp_path / "probe_gone.html", size=100, mtime=now - 100 * 86400)
    stays = _make_dump(tmp_path / "probe_stays.html", size=2048, mtime=now - 100 * 86400)
    real_stat = Path.stat

    def racing_stat(self, *a, **kw):
        if self == gone:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", racing_stat)
    listed, total = log_cmd._print_prune_plan([gone, stays])
    out = capsys.readouterr().out
    assert "probe_gone.html" not in out
    assert "probe_stays.html" in out
    assert "[INFO] Кандидаты на удаление: 1 файл(ов), освободится 2.0 КиБ" in out
    # Счётчики плана используются в prompt/[OK] — сходятся во всех окнах гонки.
    assert (listed, total) == (1, 2048)


def test_run_prune_ok_counts_only_actually_deleted(tmp_path, capsys, monkeypatch):
    """[OK] считает только реально удалённое: файл, исчезнувший между планом
    и unlink, не «удалён» — счётчики [OK] не расходятся с планом (ревью PR #923)."""
    now = time.time()
    gone = _make_dump(tmp_path / "probe_gone.html", size=100, mtime=now - 100 * 86400)
    stays = _make_dump(tmp_path / "probe_stays.html", size=2048, mtime=now - 100 * 86400)
    real_unlink = Path.unlink

    def racing_unlink(self, *a, **kw):
        if self == gone:
            raise FileNotFoundError(self)
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", racing_unlink)
    log_cmd.run(_prune_args(tmp_path, yes=True))
    out = capsys.readouterr().out
    assert "[OK] Удалено 1 файл(ов), освобождено 2.0 КиБ" in out
    assert stays.exists() is False
