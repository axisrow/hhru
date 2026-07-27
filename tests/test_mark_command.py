"""Интеграционные тесты команды mark (#13): mark.run целиком.

mark --vacancy <id> --status offer — ручная пометка оффера в responses
(hh.ru оффер как статус не отдаёт). Без браузера, только SQLite.
"""

from __future__ import annotations

import argparse
import textwrap

import pytest

from hhru_bot.commands import mark as mark_cmd
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
        "resume": "python",
        "vacancy": "v1",
        "status": "offer",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_mark_creates_offer_response(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")

    mark_cmd.run(_args(config, tmp_path / "h.db"))

    funnel = h.funnel_by_resume(since=None)[0]
    assert funnel["offer"] == 1
    out = capsys.readouterr().out
    assert "v1" in out


def test_mark_idempotent(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")

    mark_cmd.run(_args(config, tmp_path / "h.db"))
    mark_cmd.run(_args(config, tmp_path / "h.db"))  # повтор — no-op

    funnel = h.funnel_by_resume(since=None)[0]
    assert funnel["offer"] == 1


def test_mark_unknown_resume_exits(tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit) as exc:
        mark_cmd.run(_args(config, tmp_path / "h.db", resume="nope"))
    assert exc.value.code == 1


def test_mark_requires_resume(tmp_path):
    """Без --resume mark падает с понятной ошибкой (ключ per-resume)."""
    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit):
        mark_cmd.run(_args(config, tmp_path / "h.db", resume=None))


def test_mark_only_offer_status_supported(tmp_path):
    """Сейчас mark поддерживает только --status offer (верхний шаг воронки)."""
    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit):
        mark_cmd.run(_args(config, tmp_path / "h.db", status="invitation"))
