from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import reply_employers as command
from hhru_bot.history import History
from hhru_bot.negotiations_chat import ChatMessage


def _args(**overrides):
    values = dict(
        dry_run=False,
        limit=0,
        template=None,
        force=False,
        config="unused",
        history="unused",
        headless=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class _Cfg:
    storage_state_file = "unused"
    user_agent = None
    cover_letter_default = "Здравствуйте! {vacancy_title}"

    class _Throttle:
        daily_apply_limit = 100
        min_delay_seconds = 0
        max_delay_seconds = 0

    throttle = _Throttle()


class _Context:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Page:
    """Minimal page stub: goto_hh + page.content() are the only browser reads."""

    def content(self):
        return ""


def _seed_response(history: History, *, vacancy_id: str, topic: str, title: str = "Python dev"):
    history.upsert_response(vacancy_id, "Acme", "read", None, topic=topic)
    history.upsert_vacancy_seen(vacancy_id, "q", title=title)


def _patch_common(
    monkeypatch,
    history,
    *,
    page=None,
    refs=None,
    send=None,
    reader=None,
    confirmation=True,
):
    monkeypatch.setattr(command, "confirm_write", lambda *a, **k: True)
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context", lambda *a, **k: _Context(page or _Page())
    )
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *a, **k: None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda *a, **k: _Cfg())
    monkeypatch.setattr("hhru_bot.history.History", lambda *a, **k: history)
    monkeypatch.setattr("hhru_bot.negotiations_probe.topic_refs", lambda html: refs or [])
    if reader is not None:
        monkeypatch.setattr("hhru_bot.negotiations_chat.read_chat", reader)
    if send is not None:
        monkeypatch.setattr("hhru_bot.negotiations_chat.send_reply_current", send)
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.wait_reply_confirmation", lambda page, **k: confirmation
    )


# --- confirmation contract (cli-spec §1) -----------------------------------


def test_noninteractive_without_force_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(command, "confirm_write", lambda *a, **k: False)
    with pytest.raises(SystemExit) as exc:
        command.run(_args())
    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_dry_run_needs_no_confirmation(tmp_path, monkeypatch):
    """--dry-run must not even call confirm_write (works without a TTY)."""

    def _boom(*a, **k):
        raise AssertionError("confirm_write must not be called for --dry-run")

    monkeypatch.setattr(command, "confirm_write", _boom)
    history = History(tmp_path / "history.db")
    _patch_common(monkeypatch, history, refs=[])
    command.run(_args(dry_run=True))


def test_negative_limit_rejected_before_confirmation(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must fail before reaching confirm_write")

    monkeypatch.setattr(command, "confirm_write", _boom)
    with pytest.raises(SystemExit) as exc:
        command.run(_args(limit=-1))
    assert exc.value.code == 1


# --- plan from local history, zero hh.ru requests during planning ----------


def test_dry_run_prints_plan_and_sends_nothing(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="employer", inbound_marker="m1")

    def _boom_send(*a, **k):
        raise AssertionError("dry-run must never call send_reply_current")

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_boom_send,
    )

    command.run(_args(dry_run=True))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "1" in out
    with history._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 1
        row = conn.execute("SELECT status FROM replies").fetchone()
        assert row[0] == "dry_run"


def test_no_candidates_prints_info_and_returns(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _patch_common(monkeypatch, history, refs=[])
    command.run(_args(dry_run=True))
    assert "[INFO]" in capsys.readouterr().out


# --- --limit is respected ----------------------------------------------


def test_limit_restricts_number_of_chats_processed(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")
    _seed_response(history, vacancy_id="2", topic="tp2")

    class Ref1:
        topic_id = "tp1"
        chat_id = "c1"

    class Ref2:
        topic_id = "tp2"
        chat_id = "c2"

    chat = ChatMessage(author="employer", inbound_marker="m1")
    _patch_common(
        monkeypatch,
        history,
        refs=[Ref1(), Ref2()],
        reader=lambda page, topic, refs: chat,
    )

    command.run(_args(dry_run=True, limit=1))
    out = capsys.readouterr().out
    assert out.count("[DRY-RUN]") == 1


# --- fail-closed: our own last message must never get a reply --------------


def test_last_message_from_us_is_skipped_not_sent(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="me", inbound_marker="m1")

    def _boom_send(*a, **k):
        raise AssertionError("must not send when last message is ours")

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_boom_send,
    )

    command.run(_args(force=True))
    assert "[FAIL]" in capsys.readouterr().out
    with history._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_empty_chat_is_skipped(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    _patch_common(monkeypatch, history, refs=[Ref()], reader=lambda page, topic, refs: None)

    command.run(_args(dry_run=True))
    assert "[FAIL]" in capsys.readouterr().out


# --- dedup: already replied to this inbound marker --------------------------


def test_already_replied_marker_is_skipped(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")
    history.record_reply("tp1", "m1", vacancy_id="1", status="success", letter_variant=None)

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="employer", inbound_marker="m1")

    def _boom_send(*a, **k):
        raise AssertionError("must not resend to an already-replied marker")

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_boom_send,
    )

    command.run(_args(force=True))
    assert "[skip]" in capsys.readouterr().out


# --- successful send: replies + actions written in one transaction ---------


def test_successful_send_records_reply_and_action(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="employer", inbound_marker="m1")
    sent = {"called": False}

    def _send(page, text):
        sent["called"] = True

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_send,
    )

    command.run(_args(force=True))
    assert sent["called"] is True
    assert "[OK]" in capsys.readouterr().out
    with history._connect() as conn:
        reply = conn.execute("SELECT topic, inbound_marker, status FROM replies").fetchone()
        assert tuple(reply) == ("tp1", "m1", "success")
        action = conn.execute(
            "SELECT resume_id, vacancy_id, action, status FROM actions"
        ).fetchone()
        assert tuple(action) == ("", "1", "reply", "success")
    # has_replied now blocks a second send to the same inbound message.
    assert history.has_replied("tp1", "m1") is True


def test_send_failure_is_recorded_as_failed(tmp_path, monkeypatch, capsys):
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="employer", inbound_marker="m1")

    def _send(page, text):
        raise RuntimeError("не удалось однозначно найти форму ответа в чате")

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_send,
    )

    command.run(_args(force=True))
    assert "[FAIL]" in capsys.readouterr().out
    with history._connect() as conn:
        row = conn.execute("SELECT status FROM replies").fetchone()
        assert row[0] == "failed"
    # A failed send does not count as replied — retry must remain possible.
    assert history.has_replied("tp1", "m1") is False


# --- Codex review (PR #198): click confirmed but delivery unverified -------


def test_click_without_delivery_confirmation_is_recorded_as_failed(tmp_path, monkeypatch, capsys):
    """send_reply_current only clicks; a click with no delivery signal must
    not be journaled as success — otherwise has_replied() would permanently
    suppress a retry for a reply that never actually reached the employer.
    """
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    chat = ChatMessage(author="employer", inbound_marker="m1")
    sent = {"called": False}

    def _send(page, text):
        sent["called"] = True

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=lambda page, topic, refs: chat,
        send=_send,
        confirmation=False,
    )

    command.run(_args(force=True))
    assert sent["called"] is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out
    with history._connect() as conn:
        row = conn.execute("SELECT status FROM replies").fetchone()
        assert row[0] == "failed"
    # Not journaled as replied — retry must remain possible.
    assert history.has_replied("tp1", "m1") is False


# --- Codex review (PR #198): TOCTOU between planning and send --------------


def test_chat_changed_before_send_aborts_without_sending(tmp_path, monkeypatch, capsys):
    """The chat is re-read immediately before the click. If the employer's
    message is no longer the latest (e.g. we already replied from another
    device between planning and send), the command must abort instead of
    sending a stale/duplicate reply.
    """
    history = History(tmp_path / "history.db")
    _seed_response(history, vacancy_id="1", topic="tp1")

    class Ref:
        topic_id = "tp1"
        chat_id = "c1"

    planning_chat = ChatMessage(author="employer", inbound_marker="m1")
    live_chat = ChatMessage(author="me", inbound_marker="m2")
    calls = {"n": 0}

    def _reader(page, topic, refs):
        calls["n"] += 1
        # First read is the planning pass; the pre-send re-read sees a
        # chat that has moved on (we already answered from elsewhere).
        return planning_chat if calls["n"] == 1 else live_chat

    def _boom_send(page, text):
        raise AssertionError("must not send when the live chat changed before the click")

    _patch_common(
        monkeypatch,
        history,
        refs=[Ref()],
        reader=_reader,
        send=_boom_send,
    )

    command.run(_args(force=True))
    assert calls["n"] == 2
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    with history._connect() as conn:
        reply = conn.execute("SELECT topic, inbound_marker, status FROM replies").fetchone()
        # Recorded under the ORIGINAL inbound_marker seen during planning —
        # that is the message we decided to (and failed to) reply to.
        assert tuple(reply) == ("tp1", "m1", "failed")
    assert history.has_replied("tp1", "m1") is False
