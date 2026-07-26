"""Интеграционные тесты команды stats (#11): stats.run целиком.

Issue #11 требует «cmd_stats покрывается целиком». Здесь проверяем саму функцию
run() — с минимальным конфигом и seeded SQLite-историей, через захват stdout.
Без браузера (stats его не использует).
"""

from __future__ import annotations

import argparse
import textwrap

from hhru_bot.commands import stats as stats_cmd
from hhru_bot.history import History


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python developer"
    """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": str(config_path),
        "history": str(history_path),
        "resume": None,
        "period": "all",
        "format": "table",
        "list": False,
        "limit": 50,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_stats_run_summary_table_seeded(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "failed", "captcha")

    stats_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out

    # ASCII-таблица сводки: apply виден, успех=1, провал=1
    assert "apply" in out
    assert "Успех" in out  # человекочитаемый заголовок table
    assert "Итого" in out


def test_stats_run_empty_db_does_not_crash(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    stats_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out
    assert "apply" in out
    assert "0" in out  # нули на пустой истории


def test_stats_run_list_mode_md(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    stats_cmd.run(_args(config, tmp_path / "h.db", list=True, format="md", limit=10))
    out = capsys.readouterr().out
    # markdown-таблица действий
    assert "|" in out
    assert "v1" in out


def test_stats_run_csv_export_machine_readable(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    stats_cmd.run(_args(config, tmp_path / "h.db", format="csv"))
    out = capsys.readouterr().out
    # машиночитаемые имена колонок сводки
    assert out.splitlines()[0] == "action,success,dry_run,failed"


def test_stats_run_resume_filter(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")  # чужое резюме — отсечётся

    stats_cmd.run(_args(config, tmp_path / "h.db", resume="r1"))
    out = capsys.readouterr().out
    # только одно success-действие r1 (apply success = 1)
    # в ASCII-таблице apply-строка: "apply" + "1" успех
    apply_line = [ln for ln in out.splitlines() if ln.strip().startswith("| apply")][0]
    assert " 1 " in apply_line or apply_line.strip().endswith("1 |")


def test_stats_run_unknown_resume_exits(capsys, tmp_path):
    import pytest

    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit) as exc:
        stats_cmd.run(_args(config, tmp_path / "h.db", resume="does-not-exist"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "не найдено" in err
