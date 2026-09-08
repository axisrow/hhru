"""Команда robot-reply: клик по кнопке быстрых ответов робота-анкеты.

Контракты: dry-run строго до begin_action; гейт очереди (--any-topic);
uncertain после начала клика (#176); резолв robot_questionnaires только по
подтверждённому success; NoQuickReply — pre-click failed без throttle.
"""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import robot_reply as command
from hhru_bot.history import History
from hhru_bot.negotiations_chat import ChatMessage, NoQuickReply
from hhru_bot.negotiations_probe import TopicRef

pytestmark = pytest.mark.integration


def _args(tmp_path=None, **overrides):
    values = dict(
        topic="5558196272",
        answer="Нет",
        dry_run=False,
        force=True,
        any_topic=False,
        wait_ms=10,
        max_pages=1,
        config="unused",
        # Команда строит СВОЮ History по этому пути — тот же файл, что читает
        # _patch_common (ledger-утверждения видят реальные строки команды).
        history=str(tmp_path / "h.db") if tmp_path else "unused",
        headless=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class _Cfg:
    storage_state_file = "unused"
    user_agent = None

    class _Throttle:
        daily_apply_limit = 100
        min_delay_seconds = 0
        max_delay_seconds = 0

    throttle = _Throttle()


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def new_page(self):
        return object()


def _patch_common(monkeypatch, tmp_path, *, clicks=None, quick=None, confirmation=True):
    """Реальная History на tmp_path (ledger-утверждения честные); браузерная
    периферия заменена стабами."""
    history_path = tmp_path / "h.db"
    history = History(history_path)
    history.mark_robot_questionnaire(
        "5558196272", vacancy_id="136418134", reason="robot_questionnaire"
    )
    monkeypatch.setattr(command, "confirm_write", lambda *a, **k: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _Context())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda *a, **k: _Cfg())
    monkeypatch.setattr(
        "hhru_bot.negotiations_probe.paginated_topic_refs",
        lambda *a, **k: [
            TopicRef(topic_id="5558196272", chat_id="5610565556", vacancy_id="136418134")
        ],
    )
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.read_chat",
        lambda *a, **k: ChatMessage("employer", "m-robot", "Есть ли у вас опыт?"),
    )
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.find_quick_replies",
        lambda page, **k: list(quick if quick is not None else ["Да", "Нет"]),
    )

    def _click(_page, label):
        clicks.append(label)

    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.click_quick_reply",
        _click if clicks is not None else _boom_click,
    )
    monkeypatch.setattr("hhru_bot.negotiations_chat.count_visible_messages", lambda page: 3)
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.wait_reply_confirmation",
        lambda page, **k: confirmation,
    )
    monkeypatch.setattr("hhru_bot.throttle.Throttle.wait", lambda self, reason="": None)
    return history


def _boom_click(_page, _label):
    raise AssertionError("click_quick_reply не должен вызываться")


def _actions(history: History, action: str = "robot_reply"):
    with history._connect() as conn:  # noqa: SLF001 - тест читает audit напрямую
        return [
            dict(row)
            for row in conn.execute(
                "SELECT status, reason FROM actions WHERE action = ?", (action,)
            ).fetchall()
        ]


def test_dry_run_prints_plan_without_click_and_ledger(monkeypatch, tmp_path, capsys):
    clicks: list[str] = []
    history = _patch_common(monkeypatch, tmp_path, clicks=clicks)

    assert command.run(_args(tmp_path, dry_run=True)) is False

    out = capsys.readouterr().out
    assert "Есть ли у вас опыт?" in out
    assert "Кнопки: Да, Нет" in out
    assert "[DRY-RUN] Нажал бы кнопку «Нет»" in out
    assert clicks == []
    assert _actions(history) == []


def test_topic_outside_queue_refused_without_any_topic(monkeypatch, tmp_path, capsys):
    _patch_common(monkeypatch, tmp_path, clicks=[])

    assert command.run(_args(tmp_path, topic="999", any_topic=False)) is True
    assert "не в очереди robot-queue" in capsys.readouterr().out


def test_already_resolved_topic_refused(monkeypatch, tmp_path, capsys):
    history = _patch_common(monkeypatch, tmp_path, clicks=[])
    history.resolve_robot_questionnaire("5558196272", answer="Да")

    assert (
        command.run(
            _args(
                tmp_path,
            )
        )
        is True
    )
    assert "уже отвечен" in capsys.readouterr().out


def test_answer_missing_from_buttons_refused_before_ledger(monkeypatch, tmp_path, capsys):
    _patch_common(monkeypatch, tmp_path, clicks=[], quick=["Да", "Нет"])

    assert command.run(_args(tmp_path, answer="Не знаю")) is True
    out = capsys.readouterr().out
    assert "кнопки «Не знаю» нет" in out
    assert "Доступные кнопки: Да, Нет" in out


def test_successful_click_finalizes_resolves_queue_and_waits(monkeypatch, tmp_path, capsys):
    clicks: list[str] = []
    history = _patch_common(monkeypatch, tmp_path, clicks=clicks, confirmation=True)

    assert (
        command.run(
            _args(
                tmp_path,
            )
        )
        is False
    )

    out = capsys.readouterr().out
    assert "[OK] 5558196272" in out
    assert clicks == ["Нет"]
    assert _actions(history) == [{"status": "success", "reason": None}]
    assert history.is_robot_questionnaire("5558196272") is False
    assert history.robot_questionnaire_row("5558196272")["answer"] == "Нет"


def test_post_click_exception_is_uncertain_without_resolve(monkeypatch, tmp_path, capsys):
    history = _patch_common(monkeypatch, tmp_path, clicks=None, confirmation=True)
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.click_quick_reply",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network died mid-click")),
    )

    assert (
        command.run(
            _args(
                tmp_path,
            )
        )
        is True
    )

    out = capsys.readouterr().out
    assert "исход неопределён" in out
    assert _actions(history)[0]["status"] == "uncertain"
    # uncertain ≠ отвечено: строка остаётся в очереди как семафор внимания
    assert history.is_robot_questionnaire("5558196272") is True


def test_unconfirmed_delivery_is_uncertain_without_resolve(monkeypatch, tmp_path, capsys):
    history = _patch_common(monkeypatch, tmp_path, clicks=[], confirmation=False)

    assert (
        command.run(
            _args(
                tmp_path,
            )
        )
        is True
    )
    assert "не подтверждена" in capsys.readouterr().out
    assert _actions(history)[0]["status"] == "uncertain"
    assert history.is_robot_questionnaire("5558196272") is True


def test_no_quick_reply_is_pre_click_failed(monkeypatch, tmp_path, capsys):
    history = _patch_common(monkeypatch, tmp_path, clicks=None, confirmation=True)
    monkeypatch.setattr(
        "hhru_bot.negotiations_chat.click_quick_reply",
        lambda *a, **k: (_ for _ in ()).throw(NoQuickReply("Нет", available=["Да"])),
    )

    assert (
        command.run(
            _args(
                tmp_path,
            )
        )
        is True
    )

    assert "кнопка быстрых ответов" in capsys.readouterr().out
    assert _actions(history)[0]["status"] == "failed"
    assert history.is_robot_questionnaire("5558196272") is True


def test_noninteractive_without_force_is_rejected(monkeypatch, tmp_path, capsys):
    _patch_common(monkeypatch, tmp_path, clicks=[])
    monkeypatch.setattr(command, "confirm_write", lambda *a, **k: False)

    assert command.run(_args(tmp_path, force=False)) is True
    assert "Кнопка не нажата" in capsys.readouterr().out
