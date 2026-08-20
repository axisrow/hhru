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
    resolved, llm_matched = resolve_answers(
        scan, {}, known_data={"город": "Москва"}, client=FakeLLM()
    )
    assert resolved == {"Ваш город?": "Москва"}
    assert llm_matched == {"Ваш город?"}


def test_resolve_answers_marks_exact_matches_as_not_llm_derived():
    scan = FormScan([FormField("text", "#name", "Ваше имя?", True)])
    resolved, llm_matched = resolve_answers(scan, {"Ваше имя?": "Ada Lovelace"})
    assert resolved == {"Ваше имя?": "Ada Lovelace"}
    assert llm_matched == set()


def test_match_answer_llm_sends_only_keys_not_pii_values():
    """Prompt must never carry the actual profile values (#280 review: PII disclosure)."""

    class FakeLLM:
        def chat(self, messages, **_kwargs):
            content = messages[0]["content"]
            # The model is only a key-classifier: it needs to know the field
            # names exist, never the underlying contact data.
            assert "Москва" not in content
            assert "ada@example.test" not in content
            return SimpleNamespace(content='{"key":"город","confidence":0.9}')

    # "город" is not a denied (sensitive) key; contact values are also kept
    # out of the prompt even though their field names may be classified.
    facts = {"город": "Москва", "email": "ada@example.test"}
    assert match_answer_llm("В каком городе вы живёте?", facts, FakeLLM()) == "Москва"


def test_match_answer_llm_allows_contact_fields_but_denies_document_ids():
    """Contact fields are safe to infer; document identifiers still require exact answers."""

    class FakeLLM:
        def __init__(self, content):
            self.content = content

        def chat(self, _messages, **_kwargs):
            return SimpleNamespace(content=self.content)

    facts = {
        "телефон": "+7 900 123-45-67",
        "email": "ada@example.test",
        "паспорт": "0000 000000",
        "СНИЛС": "000-000-000 00",
        "ИНН": "000000000000",
        "город": "Москва",
    }
    assert (
        match_answer_llm("Ваш контакт?", facts, FakeLLM('{"key":"телефон","confidence":0.99}'))
        == "+7 900 123-45-67"
    )
    assert (
        match_answer_llm("Как связаться?", facts, FakeLLM('{"key":"email","confidence":0.99}'))
        == "ada@example.test"
    )
    for key in ("паспорт", "СНИЛС", "ИНН"):
        assert (
            match_answer_llm("Документ?", facts, FakeLLM(f'{{"key":"{key}","confidence":0.99}}'))
            is None
        )
    # Low-sensitivity fields remain matchable.
    assert (
        match_answer_llm("Ваш город?", facts, FakeLLM('{"key":"город","confidence":0.9}'))
        == "Москва"
    )


def test_match_answer_llm_degrades_on_transport_error():
    """Any client.chat() failure must fall back to None, not crash fill-form (#280 review)."""

    class FailingLLM:
        def chat(self, _messages, **_kwargs):
            raise RuntimeError("upstream unavailable")

    assert match_answer_llm("Ваш город?", {"город": "Москва"}, FailingLLM()) is None


def test_run_degrades_when_llm_client_construction_raises_any_exception(monkeypatch, tmp_path):
    """#280 review round 2: any LLMClient() construction failure (not just
    ImportError/RuntimeError/ValueError) must degrade to exact-match, not crash."""

    class FakeHistory:
        def __init__(self, _path):
            pass

        def get_profile_answers(self):
            return {"город": "Москва"}

    class FakePage:
        url = "https://forms.example.test/application"

        def goto(self, _url, *, wait_until):
            pass

        def content(self):
            return "<html></html>"

        def screenshot(self, *, path=None, full_page: bool | None = None):
            pass

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return FakePage()

    class BrokenLLMClient:
        def __init__(self, _ai_config):
            raise KeyError("malformed ai config")

    monkeypatch.setattr(fill_form, "History", FakeHistory)
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(
            storage_state_file=tmp_path / "session.json",
            user_agent=None,
            ai=SimpleNamespace(provider="fake"),
        ),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: FakeContext())
    monkeypatch.setattr(
        fill_form,
        "scan_form",
        lambda _page: SimpleNamespace(indeterminate=False, reason=""),
    )
    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", BrokenLLMClient)
    monkeypatch.setattr(fill_form, "apply_answers", lambda *_a, **_kw: (True, []))
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


def test_run_uses_account_profile_answers(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeHistory:
        def __init__(self, path):
            captured["history_path"] = path

        def get_profile_answers(self):
            return {"your name": "Ada Lovelace"}

    class FakePage:
        url = "https://forms.example.test/application"

        def goto(self, url, *, wait_until):
            captured["url"] = url
            captured["wait_until"] = wait_until

        def content(self):
            return "<html></html>"

        def screenshot(self, *, path=None, full_page: bool | None = None):
            kwargs = {"path": path, "full_page": full_page}
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


def test_run_reports_llm_matched_fields_in_dry_run_output(monkeypatch, tmp_path, capsys):
    """#280 review round 2: LLM-inferred matches must be visible to the reviewer,
    not silently indistinguishable from exact form_profile.answers matches."""

    class FakeHistory:
        def __init__(self, _path):
            pass

        def get_profile_answers(self):
            return {"город": "Москва"}

    class FakePage:
        url = "https://forms.example.test/application"

        def goto(self, _url, *, wait_until):
            pass

        def content(self):
            return "<html></html>"

        def screenshot(self, *, path=None, full_page: bool | None = None):
            pass

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
            storage_state_file=tmp_path / "session.json", user_agent=None, ai=None
        ),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: FakeContext())
    monkeypatch.setattr(
        fill_form,
        "scan_form",
        lambda _page: SimpleNamespace(indeterminate=False, reason=""),
    )
    monkeypatch.setattr(
        fill_form,
        "resolve_answers",
        lambda *_a, **_kw: ({"Ваш город?": "Москва"}, {"Ваш город?"}),
    )
    monkeypatch.setattr(fill_form, "apply_answers", lambda *_a, **_kw: (True, []))
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
    out = capsys.readouterr().out
    assert "[INFO] LLM-сопоставление" in out
    assert "Ваш город?" in out
    assert "Москва" in out
