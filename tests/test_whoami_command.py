"""Тесты команды whoami (#56, READ из спеки CLI #21).

whoami показывает действительность сессии и сводку аккаунта из ЛОКАЛЬНОЙ
истории (actions/responses), не дёргая hh.ru. Браузер не нужен — счётчики
берутся из SQLite-фикстуры. Проверяем: формат ASCII-таблицы, [INFO]-строка
валидной сессии, счётчики из history, отсутствие эмодзи, режим --resume.
"""

from __future__ import annotations

import argparse
import json
import textwrap

import pytest

from hhru_bot.commands import whoami as whoami_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config_body(storage_state: str) -> str:
    # id (slug) ≠ resume_id (число из URL). apply/bump пишут историю под
    # resume.resume_id; лимит откликов берётся из throttle.daily_apply_limit.
    return f"""
        account:
          storage_state_file: {storage_state}
        throttle:
          daily_apply_limit: 40
        resumes:
          - id: python
            resume_url: "https://hh.ru/resume/12345"
            search:
              text: "python developer"
          - id: data
            resume_url: "https://hh.ru/resume/67890"
            search:
              text: "data analyst"
    """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": str(config_path),
        "history": str(history_path),
        "resume": None,
        "headless": False,
        "online": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _valid_session(tmp_path):
    """Файл storage_state с куками — whoami считает сессию действительной."""
    p = tmp_path / "session.json"
    p.write_text(
        json.dumps({"cookies": [{"name": "hhtoken", "value": "abc"}], "origins": []}),
        encoding="utf-8",
    )
    return p


def _run(args, capsys):
    whoami_cmd.run(args)
    return capsys.readouterr().out


# --- регистрация ------------------------------------------------------------


def test_register_adds_whoami_subparser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    whoami_cmd.register(sub)
    assert "whoami" in sub.choices
    p = sub.choices["whoami"]
    opts = {a.option_strings[0] for a in p._actions if a.option_strings}
    assert "--resume" in opts
    assert "--online" in opts
    # READ-команда: --dry-run/--limit здесь бессмысленны (ничего не делает).
    assert "--dry-run" not in opts
    assert "--limit" not in opts


# --- сессия -----------------------------------------------------------------


def test_valid_session_prints_info_line(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "Локальный auth-маркер найден" in out
    assert "Сессия действительна" not in out


def test_default_check_does_not_open_browser(tmp_path, capsys, monkeypatch):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))

    def _browser_must_not_open(*args, **kwargs):
        raise AssertionError("браузер не должен открываться без --online")

    monkeypatch.setattr(whoami_cmd, "ONLINE_CHECK_URL", "https://hh.ru/applicant/resumes")
    monkeypatch.setattr("hhru_bot.browser.launch_context", _browser_must_not_open)

    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "Локальный auth-маркер найден" in out


def test_online_check_navigates_before_login_form_check(tmp_path, capsys, monkeypatch):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    events = []

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def new_page(self):
            return object()

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: Context())
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url: events.append("goto"))
    monkeypatch.setattr(
        "hhru_bot.browser.has_auth_cookie", lambda page: events.append("cookie") or True
    )
    monkeypatch.setattr(
        "hhru_bot.browser.has_login_form", lambda page: events.append("login-form") or False
    )

    out = _run(_args(cfg, tmp_path / "h.db", online=True), capsys)

    assert "проверено на hh.ru" in out
    assert events == ["goto", "cookie", "login-form"]


def test_online_check_navigation_failure_prints_fail_not_traceback(tmp_path, capsys, monkeypatch):
    """goto_hh пробрасывает PlaywrightError после исчерпания retries (#80) —
    --online не должен падать необработанным traceback (нарушение
    PageStateIndeterminate-принципа, CLAUDE.md #5): недостижимость страницы
    неопределённость, а не подтверждённый отказ, но и не молчаливый успех."""
    from playwright.sync_api import Error as PlaywrightError

    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def new_page(self):
            return object()

    def _goto_fails(page, url):
        raise PlaywrightError("net::ERR_CONNECTION_RESET")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: Context())
    monkeypatch.setattr("hhru_bot.browser.goto_hh", _goto_fails)

    out = _run(_args(cfg, tmp_path / "h.db", online=True), capsys)

    assert "[FAIL]" in out
    assert "Сессия действительна" not in out
    # Сводка из локальной истории должна напечататься несмотря на сбой сети.
    assert "Резюме" in out


def test_missing_session_prints_fail_line(tmp_path, capsys):
    cfg = _write_config(tmp_path, _config_body(str(tmp_path / "nope.json")))
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "[FAIL]" in out
    assert "Сессия действительна" not in out


def test_session_without_hhtoken_prints_fail_line(tmp_path, capsys):
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps({"cookies": [{"name": "other", "value": "abc"}], "origins": []}),
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, _config_body(str(session)))
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "[FAIL]" in out
    assert "Сессия действительна" not in out


def test_invalid_json_session_prints_fail_line(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not valid json", encoding="utf-8")
    cfg = _write_config(tmp_path, _config_body(str(broken)))
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "[FAIL]" in out


# --- сводка счётчиков -------------------------------------------------------


def test_summary_counts_from_history(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))

    h = History(tmp_path / "h.db")
    # 2 успешных отклика сегодня под resume_id "12345", 1 провал — не считается
    h.record_action("12345", "v1", "apply", "success")
    h.record_action("12345", "v2", "apply", "success")
    h.record_action("12345", "v3", "apply", "failed", "captcha")
    # ещё 1 отклик под другим резюме
    h.record_action("67890", "v4", "apply", "success")
    # приглашение из responses (account-scope)
    h.upsert_response("v1", "Acme", "invitation", "https://hh.ru/x", topic="t1")
    # новый ответ за 24ч (любой статус, сменившийся сейчас)
    h.upsert_response("v2", "Beta", "read", "https://hh.ru/y", topic="t2")

    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    # Резюме: оба slug'а
    assert "python" in out and "data" in out
    # Откликов сегодня: 3 успеха (2 + 1), формат "3 / 40"
    assert "3 / 40" in out
    # Приглашений: 1; Новых ответов 24ч: ≥2 (invitation + read).
    assert "Приглашений" in out
    assert "Новых ответов" in out


def test_summary_table_has_header_and_borders(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    assert "Поле" in out and "Значение" in out  # шапка таблицы
    assert "+" in out  # ASCII-рамка _ascii_table
    assert "Резюме" in out
    assert "Откликов сегодня" in out


def test_empty_history_shows_zero_counts(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    # пустая БД — просто создаётся History
    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    # 0 откликов / лимит
    assert "0 / 40" in out
    assert "Приглашений" in out


def test_resume_filter_counts_only_selected(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    h.record_action("67890", "v2", "apply", "success")
    h.record_action("67890", "v3", "apply", "success")

    out = _run(_args(cfg, tmp_path / "h.db", resume="data"), capsys)
    # "data" → resume_id 67890 → 2 отклика сегодня
    assert "2 / 40" in out
    # в строке Резюме фигурирует только выбранный slug
    resume_line = [ln for ln in out.splitlines() if "Резюме" in ln][0]
    assert "data" in resume_line
    assert "python" not in resume_line


# --- без эмодзи -------------------------------------------------------------


def test_output_has_no_emoji(tmp_path, capsys):
    session = _valid_session(tmp_path)
    cfg = _write_config(tmp_path, _config_body(str(session)))
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    h.upsert_response("v1", "Acme", "invitation", "https://hh.ru/x", topic="t1")

    out = _run(_args(cfg, tmp_path / "h.db"), capsys)

    # Символы эмодзи-диапазонов (U+1F000+) запрещены правилом проекта.
    for ch in out:
        code = ord(ch)
        assert not (code >= 0x1F000), f"найден эмодзи U+{code:X}: {ch!r}"
