"""Интеграционные тесты команды responses (#12): responses.run целиком.

Проверяем саму функцию run() в режиме без браузера (--since-hours 0: обход hh.ru
пропускается, выводится история ответов). Через захват stdout, с seeded SQLite-
историей responses. Браузер не поднимается — это покрывает ASCII-вывод и логику
new_responses_since без реального fetch_responses.
"""

from __future__ import annotations

import argparse
import textwrap

from hhru_bot.commands import responses as responses_cmd
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
        "max_pages": 5,
        "since_hours": 0.0,
        "headless": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_responses_run_history_only_prints_ascii_table(capsys, tmp_path):
    """--since-hours 0: нет обхода hh.ru, выводится ASCII-таблица из истории."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.upsert_response("12345", "v1", "ACME Corp", "invitation", "/c1")
    h.upsert_response("12345", "v2", "Beta LLC", "discard", "/c2")

    responses_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out

    # Заголовок секции и таблица с колонками.
    assert "Ответы работодателей" in out
    assert "Вакансия" in out
    assert "Работодатель" in out
    assert "Статус" in out
    # Статус-ключи → человекочитаемые метки (не ключи storage).
    assert "Приглашение" in out
    assert "Отказ" in out
    # Данные.
    assert "v1" in out
    assert "ACME Corp" in out
    # Рамка ASCII-таблицы (+---+).
    assert "+" in out


def test_responses_run_history_only_skips_browser(capsys, tmp_path):
    """В режиме --since-hours 0 браузер не поднимается — есть явная метка пропуска."""
    config = _write_config(tmp_path, _minimal_config())
    responses_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out
    assert "обход hh.ru пропущен" in out


def test_responses_run_empty_history_does_not_crash(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    responses_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out
    assert "нет новых ответов" in out


def test_responses_run_resume_filter(capsys, tmp_path):
    """--resume фильтрует по resume.resume_id (число из URL), как apply/bump/stats."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.upsert_response("12345", "v1", "Acme", "invitation", "/c1")
    h.upsert_response("99999", "v9", "Other", "discard", "/c9")  # чужое резюме

    responses_cmd.run(_args(config, tmp_path / "h.db", resume="python"))
    out = capsys.readouterr().out
    assert "v1" in out
    assert "v9" not in out
