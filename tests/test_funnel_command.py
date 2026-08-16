"""Интеграционные тесты команды funnel (#13): funnel.run целиком.

Команда без браузера — только SQLite + форматтер воронки. Проверяем сам run()
с минимальным конфигом и seeded историей, через захват stdout.
"""

from __future__ import annotations

import argparse
import textwrap

import pytest

from hhru_bot.commands import funnel as funnel_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    # id (slug) ≠ resume_id (число из URL). apply пишет под resume.resume_id.
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
        "format": "table",
        "period": 30,
        "dead_days": 14,
        "dead": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed(h: History):
    h.record_action("12345", "v1", "apply", "success")  # просмотрен
    h.record_action("12345", "v2", "apply", "success")  # приглашение
    h.record_action("12345", "v3", "apply", "success")  # без ответа
    h.upsert_response("v1", "Acme", "read", "/chat/v1")
    h.upsert_response("v2", "Acme", "invitation", "/chat/v2")


def test_funnel_run_table_seeded(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out

    # шаги воронки видны + значения
    assert "Отправлено" in out
    assert "Просмотрено" in out
    assert "Приглашение" in out
    assert "Оффер" in out
    assert "12345" in out
    assert "3" in out  # sent


def test_funnel_run_empty_does_not_crash(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    funnel_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out
    # даже на пустой истории не падает — шапка стабильна
    assert "Отправлено" in out


def test_funnel_run_md_format(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db", format="md"))
    out = capsys.readouterr().out
    assert "|" in out
    assert "---" in out


def test_funnel_run_resume_filter(capsys, tmp_path):
    """--resume фильтрует по resume.resume_id (как stats)."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    h.record_action("99999", "v9", "apply", "success")  # другое резюме

    funnel_cmd.run(_args(config, tmp_path / "h.db", resume="python"))
    out = capsys.readouterr().out
    assert "12345" in out
    assert "99999" not in out


def test_funnel_run_unknown_resume_exits(capsys, tmp_path):

    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit) as exc:
        funnel_cmd.run(_args(config, tmp_path / "h.db", resume="nope"))
    assert exc.value.code == 1


def test_funnel_run_dead_zone_flag(capsys, tmp_path):
    """--dead печатает «мёртвую зону» вместо/вместе с воронкой."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db", dead=True))
    out = capsys.readouterr().out
    # подпись «мёртвой зоны»
    assert "мёртв" in out.lower() or "без ответ" in out.lower()
