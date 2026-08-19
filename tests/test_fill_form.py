"""Тесты источника ответов команды fill-form."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from hhru_bot.commands import fill_form
from hhru_bot.external_forms.detect import FormField, FormScan, match_answer_llm, resolve_answers

pytestmark = pytest.mark.unit


def test_match_answer_llm_can_only_select_a_known_fact():
    class FakeLLM:
        def __init__(self, content):
            self.content = content

        def chat(self, _messages, **_kwargs):
            return SimpleNamespace(content=self.content)

    facts = {"город": "Москва", "имя": "Ada Lovelace"}
    assert (
        match_answer_llm(
            "В каком городе вы живёте?", facts, FakeLLM('{"key":"город","confidence":0.91}')
        )
        == "Москва"
    )
    assert (
        match_answer_llm("Ваш телефон?", facts, FakeLLM('{"key":"телефон","confidence":0.99}'))
        is None
    )
    assert (
        match_answer_llm("Ваш город?", facts, FakeLLM('{"key":"город","confidence":0.5}')) is None
    )


def test_resolve_answers_adds_only_confident_semantic_matches():
    class FakeLLM:
        def chat(self, messages, **_kwargs):
            assert "Ваш город?" in messages[0]["content"]
            return SimpleNamespace(content='{"key":"город","confidence":0.9}')

    scan = FormScan([FormField("text", "#city", "Ваш город?", True)])
    assert resolve_answers(scan, {}, known_data={"город": "Москва"}, client=FakeLLM()) == {
        "Ваш город?": "Москва"
    }


def test_match_answer_llm_sends_only_keys_not_pii_values():
    """Prompt must never carry the actual profile values (#280 review: PII disclosure)."""

    class FakeLLM:
        def chat(self, messages, **_kwargs):
            content = messages[0]["content"]
            # The model is only a key-classifier: it needs to know the field
            # names exist, never the underlying contact data.
            assert "+7 900 123-45-67" not in content
            assert "ada@example.test" not in content
            return SimpleNamespace(content='{"key":"телефон","confidence":0.9}')

    facts = {"телефон": "+7 900 123-45-67", "email": "ada@example.test"}
    assert match_answer_llm("Ваш телефон?", facts, FakeLLM()) == "+7 900 123-45-67"


def test_match_answer_llm_degrades_on_transport_error():
    """Any client.chat() failure must fall back to None, not crash fill-form (#280 review)."""

    class FailingLLM:
        def chat(self, _messages, **_kwargs):
            raise RuntimeError("upstream unavailable")

    assert match_answer_llm("Ваш город?", {"город": "Москва"}, FailingLLM()) is None


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
