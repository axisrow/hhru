"""Тесты источника ответов команды fill-form."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from hhru_bot.commands import fill_form

pytestmark = pytest.mark.unit


def test_run_uses_account_profile_answers(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeHistory:
        def __init__(self, path):
            captured["history_path"] = path

        def get_profile_answers(self):
            return {"your name": "Ada Lovelace"}

    class FakePage:
        url = "https://forms.example.test/application"

        def goto(self, url, wait_until):
            captured["url"] = url
            captured["wait_until"] = wait_until

        def content(self):
            return "<html></html>"

        def screenshot(self, **kwargs):
            captured["screenshot"] = kwargs

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return FakePage()

    monkeypatch.setattr(fill_form, "History", FakeHistory)
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(
            storage_state_file=tmp_path / "session.json", user_agent=None
        ),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: FakeContext())
    monkeypatch.setattr(
        fill_form,
        "scan_form",
        lambda _page: SimpleNamespace(indeterminate=False, reason=""),
    )

    def fake_apply_answers(_page, _scan, answers):
        captured["answers"] = answers
        return True, []

    monkeypatch.setattr(fill_form, "apply_answers", fake_apply_answers)
    monkeypatch.setattr(fill_form, "LOG_DIR", tmp_path / "logs")

    result = fill_form.run(
        Namespace(
            dry_run=True,
            url="https://forms.example.test/application",
            config="config.yaml",
            history=str(tmp_path / "history.db"),
            headless=True,
        )
    )

    assert result is False
    assert captured["history_path"] == str(tmp_path / "history.db")
    assert captured["answers"] == {"your name": "Ada Lovelace"}
