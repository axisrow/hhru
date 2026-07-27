"""Интеграционные тесты команды responses (#12): responses.run целиком.

Проверяем саму функцию run() в режиме без браузера (--since-hours 0: обход hh.ru
пропускается, выводится история ответов). Через захват stdout, с seeded SQLite-
историей responses (account-scope: upsert по vacancy_id без resume_id).

Браузерный путь (fetch_responses) покрывается через monkeypatch: проверяем, что
истёкшая сессия (NotAuthenticated) НЕ затирает историю и НЕ выдаёт пустой
результат за «нет новых ответов», а `--resume` игнорируется с warning.
"""

from __future__ import annotations

import argparse
import textwrap

import pytest

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
    h.upsert_response("v1", "ACME Corp", "invitation", "/c1")
    h.upsert_response("v2", "Beta LLC", "discard", "/c2")

    responses_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out

    # Заголовок секции и таблица с колонками.
    assert "Ответы работодателей" in out
    assert "Вакансия" in out
    assert "Работодатель" in out
    assert "Статус" in out
    assert "Дата" in out  # дата ответа с hh.ru (response_date)
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


def test_responses_run_resume_arg_is_ignored_with_warning(capsys, tmp_path):
    """--resume игнорируется: ответы аккаунт-уровневые, атрибуция к резюме недоступна."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "invitation", "/c1")

    responses_cmd.run(_args(config, tmp_path / "h.db", resume="python"))
    out = capsys.readouterr().out
    assert "--resume игнорируется" in out
    # история всё равно показывается (без фильтра по резюме).
    assert "v1" in out


def test_responses_run_expired_session_does_not_corrupt_history(capsys, tmp_path, monkeypatch):
    """Истёкшая сессия (NotAuthenticated): exit nonzero, история НЕ затёрта, нет «пусто».

    Регрессия Codex-critical: пустой результат выгруженной сессии не должен
    маскироваться за «нет новых ответов» — иначе приглашения скрываются молча.

    Браузер НЕ поднимается: launch_context замокан (CI не имеет Chromium), а
    fetch_responses поднимает NotAuthenticated сразу при входе в контекст.
    """
    import contextlib

    from hhru_bot.responses import NotAuthenticated

    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "invitation", "/c1")  # было ДО обхода

    class _FakeContext:
        def new_page(self):
            return object()  # page не используется — fetch падает раньше

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    def _raise(*_args, **_kwargs):
        raise NotAuthenticated("session expired")

    # НЕ запускаем реальный Chromium: патчим launch_context по источнику импорта.
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.commands.responses.fetch_responses", _raise, raising=False)
    # ленивый импорт внутри run кэшируется в sys.modules — патчим по источнику.
    monkeypatch.setattr("hhru_bot.responses.fetch_responses", _raise, raising=False)

    with pytest.raises(SystemExit) as exc:
        responses_cmd.run(_args(config, tmp_path / "h.db", since_hours=24.0))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "сессия истекла" in err or "session expired" in err
    # история цела — строка не затёрта и не добавлен «пустой» обход.
    assert h.new_responses_since(__import__("datetime").datetime.min)
