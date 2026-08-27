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

pytestmark = pytest.mark.integration


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
        "detect_external_tests": False,
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


def test_responses_alert_new_reports_invitation_and_returns_signal(capsys, tmp_path, monkeypatch):
    """--alert-new prints only new invitations and returns its scheduler code."""
    import contextlib

    from hhru_bot.exit_codes import CommandExitCode
    from hhru_bot.responses import ResponseItem, ResponseStatus

    config = _write_config(tmp_path, _minimal_config())

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    card = ResponseItem(
        vacancy_id="v-invitation", employer="ACME", status=ResponseStatus.INVITATION
    )
    fetch_kwargs = []
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.responses.fetch_responses",
        lambda *a, **k: fetch_kwargs.append(k) or [card],
    )

    result = responses_cmd.run(_args(config, tmp_path / "h.db", alert_new=True))

    assert result is CommandExitCode.NEW_INVITATIONS
    assert fetch_kwargs == [{"max_pages": 5, "strict_empty": False}]
    out = capsys.readouterr().out
    assert out == ("[INFO] Новых приглашений: 1\nВакансия: v-invitation | Работодатель: ACME\n")


def test_responses_alert_new_is_idempotent(capsys, tmp_path, monkeypatch):
    """A successful second poll with unchanged statuses is silent and exits 0."""
    import contextlib

    from hhru_bot.responses import ResponseItem, ResponseStatus

    config = _write_config(tmp_path, _minimal_config())

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    card = ResponseItem(
        vacancy_id="v-invitation", employer="ACME", status=ResponseStatus.INVITATION
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.responses.fetch_responses", lambda *a, **k: [card])

    responses_cmd.run(_args(config, tmp_path / "h.db", alert_new=True))
    capsys.readouterr()
    result = responses_cmd.run(_args(config, tmp_path / "h.db", alert_new=True))

    assert result is None
    assert capsys.readouterr().out == ""


def test_responses_alert_new_ignores_other_statuses(capsys, tmp_path, monkeypatch):
    """A newly seen response is not an invitation alert."""
    import contextlib

    from hhru_bot.responses import ResponseItem, ResponseStatus

    config = _write_config(tmp_path, _minimal_config())

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    card = ResponseItem(vacancy_id="v-response", status=ResponseStatus.RESPONSE)
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.responses.fetch_responses", lambda *a, **k: [card])

    assert responses_cmd.run(_args(config, tmp_path / "h.db", alert_new=True)) is None
    assert capsys.readouterr().out == ""


def test_sync_applied_with_zero_since_hours_still_uses_browser(capsys, tmp_path, monkeypatch):
    """Zero is a valid sync window, not a request for history-only mode."""
    import contextlib

    from hhru_bot.responses import ResponseItem, ResponseStatus

    config = _write_config(tmp_path, _minimal_config())
    seen = []

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    card = ResponseItem(vacancy_id="v1", status=ResponseStatus.READ, topic="t1", resume_id="r1")
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.commands.responses.fetch_responses",
        lambda *args, **kwargs: seen.append(kwargs) or [card],
        raising=False,
    )
    monkeypatch.setattr(
        "hhru_bot.responses.fetch_responses",
        lambda *args, **kwargs: seen.append(kwargs) or [card],
    )
    responses_cmd.run(_args(config, tmp_path / "h.db", since_hours=0.0, sync_applied=True))
    assert seen == [{"max_pages": 5, "strict_empty": True}]
    assert "добавлено 1" in capsys.readouterr().out


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


def test_responses_run_indeterminate_state_is_fail_not_traceback(capsys, tmp_path, monkeypatch):
    """#141: timeout рендера не маскируется пустым inbox и не даёт traceback."""
    import contextlib

    from hhru_bot.responses import ResponsesIndeterminate

    config = _write_config(tmp_path, _minimal_config())

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    def _raise(*_args, **_kwargs):
        raise ResponsesIndeterminate("список не подтверждён")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.responses.fetch_responses", _raise)

    with pytest.raises(SystemExit) as exc:
        responses_cmd.run(_args(config, tmp_path / "h.db", since_hours=24.0))
    assert exc.value.code == 1
    assert "список не подтверждён" in capsys.readouterr().err


def test_responses_run_skips_upsert_for_ambiguous_topic_cards(capsys, tmp_path, monkeypatch):
    """Регрессия #186 (round 3): карточка с topic_ambiguous=True (несколько SSR-topic
    кандидатов на одну вакансию, round-2 guard) НЕ должна персиститься через
    upsert_response — history матчит существующую строку по (vacancy_id,
    topic IS NULL), и запись такой карточки наравне с легитимными без-чата
    ответами слила бы разные переписки одной вакансии в одну строку истории.

    Проверяем: неоднозначная карточка пропущена (не в БД), обычная — записана,
    и печатается предупреждение с количеством пропущенных.
    """
    import contextlib

    from hhru_bot.responses import ResponseItem, ResponseStatus

    config = _write_config(tmp_path, _minimal_config())

    class _FakeContext:
        def new_page(self):
            return object()

    @contextlib.contextmanager
    def _fake_launch_context(*_args, **_kwargs):
        yield _FakeContext()

    cards = [
        ResponseItem(vacancy_id="v_ok", status=ResponseStatus.READ, topic="1"),
        ResponseItem(
            vacancy_id="v_ambiguous",
            status=ResponseStatus.READ,
            topic=None,
            topic_ambiguous=True,
        ),
    ]

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.commands.responses.fetch_responses", lambda *a, **k: cards, raising=False
    )
    monkeypatch.setattr("hhru_bot.responses.fetch_responses", lambda *a, **k: cards, raising=False)

    responses_cmd.run(_args(config, tmp_path / "h.db", since_hours=24.0))
    out = capsys.readouterr().out

    h = History(tmp_path / "h.db")
    rows = h.new_responses_since(__import__("datetime").datetime.min)
    vacancy_ids = {r["vacancy_id"] for r in rows}
    assert "v_ok" in vacancy_ids
    assert "v_ambiguous" not in vacancy_ids
    assert "Пропущено записей с неоднозначным topic: 1" in out
