"""Интеграция follow-up #81: ML-скоринг подключается к apply-циклу.

Энд-ту-энд (без браузера): ``run_apply_for_resume`` строит scoring-провайдер
при наличии ai + ai_profile (через _common._build_scoring_provider) и передаёт
его в ``rank_candidates`` с llm_shortlist=10 (анти-фрод). Без AI — провайдер
None (эвристика #15, обратная совместимость). ImportError openai → None.

Браузер/search/apply подменяются; реальный конфиг, реальное построение
провайдера. Образец — test_probe_letter_integration.py (#54).
"""

from __future__ import annotations

import argparse

from hhru_bot.commands import _common
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.config_sections.ai import AiConfig
from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.search import VacancyCard


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
    return argparse.Namespace(
        config=None,
        resume=None,
        dry_run=True,
        headless=True,
        max_pages=1,
        limit=0,
    )


def _fake_cards() -> list[VacancyCard]:
    return [
        VacancyCard(
            vacancy_id="1", title="Python Dev", company="Acme", url="https://hh.ru/vacancy/1"
        ),
        VacancyCard(
            vacancy_id="2", title="Senior Python", company="Beta", url="https://hh.ru/vacancy/2"
        ),
    ]


def _stub_apply_cycle(monkeypatch, captured):
    """Подменяет search (карточки) и apply (без браузера); шпионит rank_candidates."""
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: _fake_cards())  # noqa: ARG005
    # apply_to_vacancy не должен зваться при провайдере None/AI на уровне wiring,
    # но подменяем на всякий случай — ранжирование тестируем через spy.
    monkeypatch.setattr(
        _common,
        "apply_to_vacancy",
        lambda *a, **k: _StubResult(),  # noqa: ARG005
    )

    real_rank = _common.rank_candidates

    def _spy_rank(candidates, filters, resume, scoring_provider=None, **kwargs):  # noqa: ARG001
        captured["scoring_provider"] = scoring_provider
        captured["llm_shortlist"] = kwargs.get("llm_shortlist")
        return real_rank(candidates, filters, resume, scoring_provider=scoring_provider)

    monkeypatch.setattr(_common, "rank_candidates", _spy_rank)


class _StubResult:
    success = False
    reason = "stub"
    letter_variant = None
    skipped = False  # #95: совместимость с result.skipped в _common.run_apply_for_resume


def test_run_apply_passes_llm_provider_when_ai_on(tmp_path, monkeypatch):
    """AI включён → rank_candidates получает LLMScoringProvider + llm_shortlist=10."""
    from hhru_bot.scoring import LLMScoringProvider

    class _FakeLLM:
        def __init__(self, cfg):  # noqa: ANN001, ARG002
            pass

        def chat(self, messages, **params):  # noqa: ANN001, ARG002
            from hhru_bot.ai.types import NormalizedResponse

            return NormalizedResponse(
                content='{"score": 80, "rationale": "ok", "factors": {}}',
                tool_calls=None,
                finish_reason="stop",
            )

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", lambda cfg: _FakeLLM(cfg))

    captured: dict = {}
    _stub_apply_cycle(monkeypatch, captured)

    config = _config_with_ai(tmp_path, _resume_with_profile())
    history = _make_history(tmp_path)
    throttle = _make_throttle(config, history)

    _common.run_apply_for_resume(
        object(), config, _resume_with_profile(), history, throttle, _apply_args()
    )

    assert isinstance(captured["scoring_provider"], LLMScoringProvider)
    assert captured["llm_shortlist"] == 10


def test_run_apply_passes_none_when_ai_off(tmp_path, monkeypatch):
    """AI выключен → rank_candidates получает None (эвристика #15, совместимость)."""
    captured: dict = {}
    _stub_apply_cycle(monkeypatch, captured)

    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[_resume_without_profile()],
        ai=None,
    )
    history = _make_history(tmp_path)
    throttle = _make_throttle(config, history)

    _common.run_apply_for_resume(
        object(), config, _resume_without_profile(), history, throttle, _apply_args()
    )

    assert captured["scoring_provider"] is None


def test_run_apply_passes_none_on_import_error(tmp_path, monkeypatch):
    """openai не установлен → LLMClient поднимает ImportError → провайдер None.

    Сценарий: LLMClient(ai_config) тянет openai при construction (lazy-импорт
    внутри модуля). Если openai нет — ImportError. _build_scoring_provider ловит
    его и откатывается на None (эвристику), как _build_letter_provider (#17).
    """

    def _raise_import_error(cfg):  # noqa: ANN001, ARG001
        raise ImportError("No module named 'openai'")

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient", _raise_import_error)

    captured: dict = {}
    _stub_apply_cycle(monkeypatch, captured)

    config = _config_with_ai(tmp_path, _resume_with_profile())
    history = _make_history(tmp_path)
    throttle = _make_throttle(config, history)

    _common.run_apply_for_resume(
        object(), config, _resume_with_profile(), history, throttle, _apply_args()
    )

    assert captured["scoring_provider"] is None


def _make_history(tmp_path):
    from hhru_bot.history import History

    return History(tmp_path / "history.db")


def _make_throttle(config, history):
    from hhru_bot.throttle import Throttle

    return Throttle(config.throttle, history)
