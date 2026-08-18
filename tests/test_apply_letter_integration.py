"""Интеграция #17: провайдер писем прокидывается через run_apply_for_resume.

Энд-ту-энд (без браузера): _build_letter_provider строит AI-провайдер при
наличии ai + ai_profile; letter_variant доходит до history. Без AI — None
(статичный шаблон), variant='template'. Подмена только браузерного сбора
карточек и LLM-вызова; реальная History, реальный pipeline.
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.commands import _common
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.config_sections.ai import AiConfig
from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.history import History
from hhru_bot.search import VacancyCard
from hhru_bot.throttle import Throttle

pytestmark = pytest.mark.integration


class _FakeLocator:
    @property
    def first(self):
        return self

    def __init__(self, present: bool = False):
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, **_kwargs) -> None:
        return None

    def fill(self, _value: str) -> None:
        return None

    def get_attribute(self, _name: str) -> str | None:  # noqa: ARG002
        return None

    def nth(self, _i: int) -> _FakeLocator:  # noqa: ARG002
        return self

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() комбинирует apply-button и
        # already-responded-маркеры одним локатором.
        return _FakeLocator(present=self._present or other._present)

    def filter(self, *, visible: bool | None = None) -> _FakeLocator:  # noqa: ARG002
        # #248 cycle-review round 2: dedup.check_already_responded() narrows the
        # union to visible matches before .first — the fake has no hidden-vs-
        # visible distinction, so filtering is a no-op here.
        return self


class _ApplyFakePage:
    """Page для apply-pipeline в dry-run: есть кнопка отклика, до формы не доходим."""

    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.selector_groups import vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=True)
        return _FakeLocator(present=False)

    def wait_for_url(self, _url_pattern, **_kwargs):  # noqa: ARG002
        # #179: navigate_to_response_form больше не использует expect_navigation.
        # Этот путь — dry-run, до формы не доходим (см. докстринг класса), метод
        # не должен вызываться, но определён для консистентности с реальным Page.
        return None


def _resume_with_profile() -> ResumeConfig:
    return ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
        ai_profile=AIProfile(
            summary="Бэкенд-разработчик",
            skills=["python", "django"],
            desired_role="Senior Python Developer",
        ),
    )


def _resume_without_profile() -> ResumeConfig:
    return ResumeConfig(
        id="plain",
        resume_url="https://hh.ru/resume/BBB222",
        search=SearchFilters(text="python developer"),
    )


def _config_with_ai(tmp_path, resume) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[resume],
        ai=AiConfig(provider="openai", model="gpt-4o", base_url="https://api.openai.com/v1"),
    )


def _apply_args() -> argparse.Namespace:
    return argparse.Namespace(dry_run=True, limit=1, max_pages=5, headless=True)


def _read_letter_variants(history_db) -> list[str | None]:
    conn = sqlite3.connect(history_db)
    try:
        return [r[0] for r in conn.execute("SELECT letter_variant FROM actions ORDER BY id")]
    finally:
        conn.close()


# --- _build_letter_provider: AI включён/выключен ---


def test_build_letter_provider_returns_ai_when_ai_and_profile_present(tmp_path, monkeypatch):
    # Подменяем LLMClient, чтобы не тянуть реальный openai/сеть.
    class _FakeLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            return NormalizedResponse(content="AI письмо", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", lambda cfg: _FakeLLM())

    config = _config_with_ai(tmp_path, _resume_with_profile())
    provider = _common._build_letter_provider(config, config.resumes[0], "шаблон {vacancy_title}")
    assert provider is not None
    from hhru_bot.ai.letters import AICoverLetterProvider

    assert isinstance(provider, AICoverLetterProvider)


def test_build_letter_provider_none_without_ai_config(tmp_path):
    # Нет секции ai (top-level) → None, даже если есть ai_profile.
    resume = _resume_with_profile()
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="x",
        resumes=[resume],
        ai=None,
    )
    assert _common._build_letter_provider(config, resume, "x") is None


def test_build_letter_provider_none_without_profile(tmp_path):
    # Есть ai, но нет ai_profile → None (нет данных кандидата для промпта).
    resume = _resume_without_profile()
    config = _config_with_ai(tmp_path, resume)
    assert _common._build_letter_provider(config, resume, "x") is None


def test_build_letter_provider_none_when_openai_missing(tmp_path, monkeypatch):
    # [ai] не установлен → LLMClient бросает ImportError → None (молчаливый
    # fallback на шаблон, обычный отклик не падает).
    def _boom(_cfg):
        raise ImportError("openai SDK is required")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", _boom)
    config = _config_with_ai(tmp_path, _resume_with_profile())
    assert _common._build_letter_provider(config, config.resumes[0], "x") is None


# --- letter_variant доходит до history через run_apply_for_resume ---


def test_apply_writes_ai_variant_to_history(tmp_path, monkeypatch):
    # AI включён и срабатывает → variant='ai' в actions.
    class _FakeLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            return NormalizedResponse(content="AI письмо", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", lambda cfg: _FakeLLM())
    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )

    resume = _resume_with_profile()
    history_db = tmp_path / "history.db"
    history = History(history_db)
    throttle = Throttle(ThrottleConfig(), history)
    config = _config_with_ai(tmp_path, resume)

    _common.run_apply_for_resume(_ApplyFakePage(), config, resume, history, throttle, _apply_args())

    assert _read_letter_variants(history_db) == ["ai"]


def test_apply_writes_template_variant_without_ai(tmp_path, monkeypatch):
    # AI выключен → статичный шаблон, variant='template'.
    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )

    resume = _resume_without_profile()
    history_db = tmp_path / "history.db"
    history = History(history_db)
    throttle = Throttle(ThrottleConfig(), history)
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[resume],
        ai=None,
    )

    _common.run_apply_for_resume(_ApplyFakePage(), config, resume, history, throttle, _apply_args())

    assert _read_letter_variants(history_db) == ["template"]


def test_apply_writes_ai_fallback_variant_when_llm_fails(tmp_path, monkeypatch):
    # LLM падает в рантайме → провайдер откатывается на шаблон, variant='ai_fallback'.
    class _FailingLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            raise ConnectionError("LLM недоступен")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", lambda cfg: _FailingLLM())
    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )

    resume = _resume_with_profile()
    history_db = tmp_path / "history.db"
    history = History(history_db)
    throttle = Throttle(ThrottleConfig(), history)
    config = _config_with_ai(tmp_path, resume)

    _common.run_apply_for_resume(_ApplyFakePage(), config, resume, history, throttle, _apply_args())

    assert _read_letter_variants(history_db) == ["ai_fallback"]
