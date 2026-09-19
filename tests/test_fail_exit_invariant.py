"""Общий инвариант exit-кодов: [FAIL] в stdout -> ненулевой exit (#1141).

Три слоя, как в test_auth_gate:
чистый сканер FailWatchStream (детект/ложные срабатывания/разбиение чанков),
граница cli.main (list-resumes при невалидной сессии — exit 1, [OK] — без
SystemExit) и census при форме входа — typed SESSION_EXPIRED (78).

Живых прогонов нет: сессия имитируется отсутствующим файлом storage_state,
браузер census подменяется, как в test_auth_gate (#1129).
"""

from __future__ import annotations

import argparse
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.commands.census as census_cmd
import hhru_bot.commands.log_cmd as log_cmd
from hhru_bot.cli import main
from hhru_bot.exit_codes import FAIL_JUDGE_WINDOW, CommandExitCode, FailWatchStream

pytestmark = pytest.mark.integration


# --- FailWatchStream: чистый сканер ------------------------------------------


def _watch() -> tuple[FailWatchStream, io.StringIO]:
    buffer = io.StringIO()
    return FailWatchStream(buffer), buffer


def test_stream_passes_writes_through():
    watch, buffer = _watch()
    assert watch.write("[OK] поднято\n") == len("[OK] поднято\n")
    assert buffer.getvalue() == "[OK] поднято\n"


def test_stream_detects_fail_at_line_start():
    watch, _ = _watch()
    watch.write("[FAIL] Сессия недействительна. Выполните login.\n")
    assert watch.saw_fail_line


def test_stream_detects_indented_fail_batch_line():
    watch, _ = _watch()
    watch.write("  [FAIL] 00001 — кнопка отклика не найдена\n")
    assert watch.saw_fail_line


def test_stream_ignores_fail_inside_line_and_table_cell():
    watch, _ = _watch()
    watch.write("упоминание [FAIL] внутри строки — не вердикт\n")
    watch.write("| [FAIL] ячейка ASCII-таблицы |\n")
    assert not watch.saw_fail_line


def test_stream_detects_fail_split_across_writes():
    watch, _ = _watch()
    watch.write("[FA")
    watch.write("IL] cookie hhtoken не найден\n")
    assert watch.saw_fail_line


def test_stream_no_false_positive_when_split_inside_plain_line():
    watch, _ = _watch()
    watch.write("ok [FA")
    watch.write("IL] не в начале строки\n")
    assert not watch.saw_fail_line


def test_stream_long_line_judged_once_without_false_positive():
    watch, _ = _watch()
    # Строка длиннее окна без \n: судится по голове («не [FAIL]»), продолжение
    # той же строки в следующих чанках новую строку не открывает.
    watch.write("x" * (FAIL_JUDGE_WINDOW + 50))
    watch.write("[FAIL] это продолжение той же длинной строки\n")
    assert not watch.saw_fail_line
    watch.write("[FAIL] а это уже новая строка\n")
    assert watch.saw_fail_line


def test_stream_long_fail_line_detected_by_head():
    watch, _ = _watch()
    watch.write("[FAIL] " + "x" * 500 + "\n")
    assert watch.saw_fail_line


def test_stream_is_transparent_for_stream_attributes():
    buffer = io.StringIO()
    watch = FailWatchStream(buffer)
    assert watch.getvalue() == buffer.getvalue()
    assert not watch.isatty()


# --- граница cli.main: [FAIL] -> 1, [OK] -> без SystemExit --------------------


def _config(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


ACCOUNT_BODY = """
account:
  storage_state_file: {session}
"""


def test_cli_list_resumes_fail_session_exits_nonzero(monkeypatch, tmp_path):
    """Точный кейс #1141: list-resumes печатает [FAIL] о сессии — exit 1."""
    body = ACCOUNT_BODY.format(session=str(tmp_path / "missing_session.json"))
    argv = [
        "--config",
        _config(tmp_path, body),
        "--history",
        str(tmp_path / "history.db"),
        "list-resumes",
    ]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 1


def test_cli_list_resumes_local_ok_exits_zero(monkeypatch, tmp_path):
    """[OK]-завершение (--local, без hh.ru) не должно давать SystemExit."""
    body = ACCOUNT_BODY.format(session=str(tmp_path / "session.json"))
    argv = [
        "--config",
        _config(tmp_path, body),
        "--history",
        str(tmp_path / "history.db"),
        "list-resumes",
        "--local",
    ]
    assert main(argv) is None


def test_cli_log_command_excluded_from_invariant(monkeypatch, tmp_path):
    """`log` печатает ЧУЖИЕ строки прошлых прогонов — [FAIL] в них не вердикт."""
    log_file = tmp_path / "hhru_bot.log"
    log_file.write_text("[FAIL] строка прошлого прогона\n", encoding="utf-8")
    monkeypatch.setattr(log_cmd, "DEFAULT_LOG_PATH", log_file)
    assert main(["log", "-n", "5"]) is None


# --- census при форме входа: typed SESSION_EXPIRED ----------------------------


def _patch_census(monkeypatch, *, login_form: bool):
    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(census_cmd, "launch_context", launch)
    monkeypatch.setattr(census_cmd, "goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr(census_cmd, "rendered_controls_census", lambda page: [])
    monkeypatch.setattr(census_cmd, "has_login_form", lambda page: login_form)


def test_cli_census_login_form_exits_session_expired(monkeypatch, tmp_path):
    _patch_census(monkeypatch, login_form=True)
    argv = [
        "--config",
        _config(tmp_path, ACCOUNT_BODY.format(session=str(tmp_path / "s.json"))),
        "census",
        "--url",
        "https://hh.ru/applicant/my_resumes",
    ]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == CommandExitCode.SESSION_EXPIRED.value


def test_cli_census_target_page_exits_zero(monkeypatch, tmp_path):
    _patch_census(monkeypatch, login_form=False)
    argv = [
        "--config",
        _config(tmp_path, ACCOUNT_BODY.format(session=str(tmp_path / "s.json"))),
        "census",
        "--url",
        "https://hh.ru/applicant/my_resumes",
    ]
    assert main(argv) is None


def test_census_run_returns_typed_code_for_callers(monkeypatch, tmp_path):
    """run() возвращает typed-код и напрямую (для тестов/вызовов мимо main)."""
    _patch_census(monkeypatch, login_form=True)
    args = argparse.Namespace(
        config=_config(tmp_path, ACCOUNT_BODY.format(session=str(tmp_path / "s.json"))),
        headless=True,
        user_agent=None,
        url="https://hh.ru/x",
        json=False,
        wait_ms=0,
        fill_text=None,
    )
    assert census_cmd.run(args) is CommandExitCode.SESSION_EXPIRED
