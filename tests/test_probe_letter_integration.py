"""Интеграция follow-up #54: AI-письмо прокидывается в probe-команду.

Энд-ту-энд (без браузера): команда ``probe`` строит AI-провайдер при наличии
ai + ai_profile (через _common._build_letter_provider) и передаёт его в
probe_vacancy. Без AI — провайдер None (статичный шаблон). Браузер и сам
probe_vacancy подменяются; реальный конфиг, реальное построение провайдера.

Критерий готовности #54: ``probe --vacancy-id N`` с включённым AI показывает
в дампе формы письмо от LLM. Здесь это проверяется на уровне проброса
провайдера в probe_vacancy (текст письма/дамп тестируются в test_apply_probe).
"""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import probe as probe_cmd
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.config_sections.ai import AiConfig
from hhru_bot.config_sections.ai_profile import AIProfile


class _CtxStub:
    """Заглушка браузерного контекста: context manager + new_page()->None."""

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_exc):  # noqa: ANN204
        return False

    def new_page(self):  # noqa: ANN204
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


def _probe_args(resume_id: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        config=None,
        resume=resume_id,
        headless=True,
        vacancy_id="42",
        vacancy_url=None,
    )


def test_probe_command_passes_ai_provider_to_probe_vacancy(tmp_path, monkeypatch):
    """AI включён → probe_vacancy получает не-None AICoverLetterProvider."""
    from hhru_bot.ai.letters import AICoverLetterProvider

    class _FakeLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            from hhru_bot.ai.types import NormalizedResponse

            return NormalizedResponse(content="AI письмо", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", lambda cfg: _FakeLLM())
    # launch_context/probe_vacancy/load_config_or_exit — лениво импортируются
    # внутри run(), поэтому патчим их в модулях-источниках (имя резолвится
    # из источника в момент вызова).
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context",
        lambda *a, **k: _CtxStub(),  # noqa: ARG005
    )

    captured: dict = {}

    def _spy_probe_vacancy(page, vacancy, resume_id, cover_letter_template, **kwargs):  # noqa: ARG001
        captured["letter_provider"] = kwargs.get("letter_provider")
        # Возвращаем провал, чтобы не идти в реальный дамп.
        from hhru_bot.apply.probe import ProbeResult
        from hhru_bot.search import VacancyCard

        return ProbeResult(
            VacancyCard(vacancy_id="42", title="t", company="c", url="https://hh.ru/vacancy/42"),
            False,
            "stub",
        )

    monkeypatch.setattr("hhru_bot.apply.probe.probe_vacancy", _spy_probe_vacancy)

    config = _config_with_ai(tmp_path, _resume_with_profile())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _cfg: config)

    probe_cmd.run(_probe_args("python"))

    provider = captured["letter_provider"]
    assert provider is not None
    assert isinstance(provider, AICoverLetterProvider)


def test_probe_command_passes_none_provider_without_ai(tmp_path, monkeypatch):
    """AI выключен → probe_vacancy получает None (статичный шаблон)."""
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context",
        lambda *a, **k: _CtxStub(),  # noqa: ARG005
    )

    captured: dict = {}

    def _spy_probe_vacancy(page, vacancy, resume_id, cover_letter_template, **kwargs):  # noqa: ARG001
        captured["letter_provider"] = kwargs.get("letter_provider")
        from hhru_bot.apply.probe import ProbeResult
        from hhru_bot.search import VacancyCard

        return ProbeResult(
            VacancyCard(vacancy_id="42", title="t", company="c", url="https://hh.ru/vacancy/42"),
            False,
            "stub",
        )

    monkeypatch.setattr("hhru_bot.apply.probe.probe_vacancy", _spy_probe_vacancy)

    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[_resume_without_profile()],
        ai=None,
    )
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _cfg: config)

    probe_cmd.run(_probe_args("plain"))

    assert captured["letter_provider"] is None


def test_probe_command_no_resume_exits(tmp_path, monkeypatch, capsys):
    """Без --resume команда probe завершается с ошибкой (нет резюме для дампа)."""
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="x",
        resumes=[],
        ai=None,
    )

    # load_config возвращает конфиг без резюме; resolve_resumes → [].
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _cfg: config)

    with pytest.raises(SystemExit) as excinfo:
        probe_cmd.run(_probe_args(None))

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "не выбрано резюме" in err.lower()
