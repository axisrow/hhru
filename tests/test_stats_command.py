"""Интеграционные тесты команды stats (#11): stats.run целиком.

Issue #11 требует «cmd_stats покрывается целиком». Здесь проверяем саму функцию
run() — с минимальным конфигом и seeded SQLite-историей, через захват stdout.
Без браузера (stats его не использует).
"""

from __future__ import annotations

import argparse
import io
import textwrap

import pytest

from hhru_bot.commands import stats as stats_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    # Реалистичный контракт: id (slug) ≠ resume_id (число из URL). apply/bump
    # пишут историю под resume.resume_id (число), а --resume в CLI получает slug.
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: python
            resume_url: "https://hh.ru/resume/12345"
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
    # машиночитаемые имена колонок сводки (#176: добавлена колонка uncertain —
    # действие могло выполниться при упавшем посреди клика Playwright)
    assert out.splitlines()[0] == "action,success,dry_run,failed,uncertain"


def test_stats_run_csv_export_is_single_valid_csv_document(capsys, tmp_path):
    """CSV-режим — экспорт для машин: один документ, одна схема колонок.

    Регрессия #112 (reply-аналитика в stats): второй печатаемый блок
    (format_replies) не должен начинать в том же stdout-потоке новый
    CSV-документ с другим набором колонок (metric,value) — консьюмер,
    парсящий stdout одним csv.reader, увидит смешение схем и упадёт/
    получит битые данные."""
    import csv as csv_module

    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    stats_cmd.run(_args(config, tmp_path / "h.db", format="csv"))
    out = capsys.readouterr().out

    rows = list(csv_module.reader(io.StringIO(out)))
    header = rows[0]
    for row in rows[1:]:
        assert len(row) == len(header), (
            f"CSV-строка {row!r} не соответствует схеме заголовка {header!r} — "
            "второй документ (reply-сводка) не должен ломать единую CSV-схему"
        )


def test_stats_run_resume_filter(capsys, tmp_path):
    """--resume фильтрует по resume.resume_id (число из URL), как apply/bump.

    Регрессия: PR #31 унифицировал ключ истории на resume.resume_id. stats.run
    получает slug из --resume и должен резолвить его в resume.resume_id для
    фильтра — иначе сводка всегда пуста."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    # apply/bump пишут под resume.resume_id (= "12345" из URL), НЕ под slug "python"
    h.record_action("12345", "v1", "apply", "success")
    h.record_action("99999", "v9", "apply", "success")  # другое резюме — отсечётся

    # --resume получает slug "python", stats должен найти запись 12345
    stats_cmd.run(_args(config, tmp_path / "h.db", resume="python"))
    out = capsys.readouterr().out
    apply_line = [ln for ln in out.splitlines() if ln.strip().startswith("| apply")][0]
    assert " 1 " in apply_line or apply_line.strip().endswith("1 |")


def test_stats_run_unknown_resume_exits(capsys, tmp_path):

    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit) as exc:
        stats_cmd.run(_args(config, tmp_path / "h.db", resume="does-not-exist"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "не найдено" in err
