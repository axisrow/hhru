"""Тесты ML-скоринга: classify_employer + LLMScoringProvider (issue #74).

Чистая логика без браузера. Существующий test_scoring.py покрывает эвристику
#15 (факторы по title/ключевым словам); здесь — новые компоненты #74:
  - classify_employer: гиганты (RU top tech/big corp/global), эвристики
    trusted/reviews_count, unknown для мелких.
  - _parse_llm_score: валидный JSON (с/без markdown-обёртки), None/пусто/
    плохой JSON/score вне [0,100] → None.
  - LLMScoringProvider: успех (score 0-100 + rationale), None-контент →
    fallback, исключение из chat → fallback, плохой JSON → fallback. Ни один
    сценарий не бросает (инвариант, как AICoverLetterProvider #17).

Мокаем только LLMClient.chat (контракт #16 → NormalizedResponse.content),
как test_apply_letter. ОткрытыйAI/openai НЕ нужен.
"""

from __future__ import annotations

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config import SearchFilters
from hhru_bot.config_sections.scoring import ScoringWeights
from hhru_bot.scoring import (
    EmployerInfo,
    HeuristicScoringProvider,
    KnownCompanyTier,
    LLMScoringProvider,
    _parse_llm_score,
    classify_employer,
)
from hhru_bot.search import VacancyCard

# --- хелперы ----------------------------------------------------------------


def card(
    vacancy_id: str = "1",
    title: str = "Python Developer",
    company: str = "ООО Ромашка",
    employer_info: EmployerInfo | None = None,
) -> VacancyCard:
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=title,
        company=company,
        url="https://hh.ru/vacancy/0",
        employer_info=employer_info,
    )


def heuristic_provider(weights: ScoringWeights | None = None) -> HeuristicScoringProvider:
    """Эвристический провайдер с заданными весами (дефолт — факторы #15)."""
    filters = SearchFilters(text="python", must_have=["django"])
    return HeuristicScoringProvider(filters, (weights or ScoringWeights()))


class _RecordingLLM:
    """Мок LLMClient.chat(messages, **params) -> NormalizedResponse (#16 контракт)."""

    def __init__(self, content: str | None, finish_reason: str = "stop"):
        self._content = content
        self._finish_reason = finish_reason
        self.calls: list[tuple[list, dict]] = []

    def chat(self, messages, **params):
        self.calls.append((messages, params))
        return NormalizedResponse(
            content=self._content, tool_calls=None, finish_reason=self._finish_reason
        )


class _FailingLLM:
    """Мок LLMClient, бросающий при chat (сеть/таймаут/SDK-исключение)."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def chat(self, messages, **params):  # noqa: ARG002
        raise self.exc


# --- classify_employer: lookup по списку гигантов (Этап 2) -------------------


def test_classify_yandex_is_top_tech():
    assert classify_employer("Яндекс") == KnownCompanyTier.TOP_TECH


def test_classify_sber_is_top_tech():
    assert classify_employer("ООО Сбербанк") == KnownCompanyTier.TOP_TECH


def test_classify_substring_matches_brand_inside_legal_entity():
    # «Яндекс.Такси» / «Yandex LLC» — бренд внутри названия юр. лица.
    assert classify_employer("Яндекс.Такси") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Yandex LLC") == KnownCompanyTier.TOP_TECH


def test_classify_top_tech_brands():
    for name in ("VK", "ООО Озон", "Т-Банк", "Авито", "HeadHunter"):
        assert classify_employer(name) == KnownCompanyTier.TOP_TECH, name


def test_classify_global_faang_are_top_tech():
    assert classify_employer("Google") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Amazon Web Services") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Microsoft") == KnownCompanyTier.TOP_TECH


def test_classify_big_corp_second_line():
    # МТС/Альфа/ВТБ — крупный бизнес, но не top tech.
    assert classify_employer("МТС") == KnownCompanyTier.BIG_CORP
    assert classify_employer("Альфа-Банк") == KnownCompanyTier.BIG_CORP
    assert classify_employer("ВТБ") == KnownCompanyTier.BIG_CORP


def test_classify_unknown_company_without_info():
    assert classify_employer("ООО Ромашка") == KnownCompanyTier.UNKNOWN


def test_classify_empty_name_unknown():
    assert classify_employer("") == KnownCompanyTier.UNKNOWN
    assert classify_employer(None) == KnownCompanyTier.UNKNOWN


# --- classify_employer: эвристики по info из карточки (Этап 1 + 2) ----------


def test_classify_trusted_employer_is_big_corp():
    # Бейдж «надёжный работодатель» от hh.ru — сильный сигнал известности.
    info = EmployerInfo(rating=4.5, reviews_count=10, trusted=True)
    assert classify_employer("Неизвестная Контора", info) == KnownCompanyTier.BIG_CORP


def test_classify_many_reviews_is_mid():
    info = EmployerInfo(reviews_count=500)
    assert classify_employer("Средний Бизнес", info) == KnownCompanyTier.MID


def test_classify_few_reviews_is_unknown():
    info = EmployerInfo(reviews_count=10)
    assert classify_employer("ООО Стартап", info) == KnownCompanyTier.UNKNOWN


def test_classify_top_tech_wins_over_heuristics():
    # Даже без отзывов и без trusted — Яндекс остаётся top_tech.
    info = EmployerInfo(reviews_count=0, trusted=False)
    assert classify_employer("Яндекс", info) == KnownCompanyTier.TOP_TECH


# --- _parse_llm_score: разбор JSON-ответа LLM -------------------------------


def test_parse_llm_score_valid_json():
    content = '{"score": 87, "rationale": "Хорошее совпадение стека", "factors": {"skills": 40}}'
    outcome = _parse_llm_score(content, card())
    assert outcome is not None
    assert outcome.score_0_100 == 87.0
    assert "Хорошее совпадение" in outcome.rationale
    assert outcome.breakdown == {"skills": 40.0}


def test_parse_llm_score_json_in_markdown_fence():
    content = '```json\n{"score": 50, "rationale": "Средне"}\n```'
    outcome = _parse_llm_score(content, card())
    assert outcome is not None
    assert outcome.score_0_100 == 50.0


def test_parse_llm_score_none_content_returns_none():
    assert _parse_llm_score(None, card()) is None


def test_parse_llm_score_empty_content_returns_none():
    assert _parse_llm_score("", card()) is None
    assert _parse_llm_score("   ", card()) is None


def test_parse_llm_score_non_json_returns_none():
    assert _parse_llm_score("Не удалось оценить вакансию.", card()) is None


def test_parse_llm_score_malformed_json_returns_none():
    assert _parse_llm_score('{"score": ', card()) is None


def test_parse_llm_score_score_out_of_range_returns_none():
    # score вне [0, 100] → невалидный ответ → точка fallback.
    assert _parse_llm_score('{"score": 150}', card()) is None
    assert _parse_llm_score('{"score": -5}', card()) is None


def test_parse_llm_score_non_numeric_score_returns_none():
    assert _parse_llm_score('{"score": "высокий"}', card()) is None


def test_parse_llm_score_factors_optional():
    outcome = _parse_llm_score('{"score": 70}', card())
    assert outcome is not None
    assert outcome.score_0_100 == 70.0
    assert outcome.breakdown == {}


def test_parse_llm_score_non_numeric_factors_dropped():
    outcome = _parse_llm_score('{"score": 70, "factors": {"ok": 10, "bad": "x"}}', card())
    assert outcome is not None
    assert outcome.breakdown == {"ok": 10.0}


# --- LLMScoringProvider: успех + fallback (инвариант: никогда не бросает) ----


def test_llm_provider_success_returns_score_and_rationale():
    llm = _RecordingLLM('{"score": 92, "rationale": "Идеальный матч", "factors": {"stack": 60}}')
    provider = LLMScoringProvider(llm_client=llm, fallback=heuristic_provider())
    outcome = provider.score(card(company="Яндекс"))
    assert outcome.score_0_100 == 92.0
    assert "Идеальный матч" in outcome.rationale
    assert outcome.breakdown == {"stack": 60.0}
    # LLM реально вызывался; промпт содержал контекст вакансии.
    assert llm.calls
    prompt = str(llm.calls[0][0])
    assert "Python Developer" in prompt


def test_llm_provider_none_content_falls_back_to_heuristic():
    llm = _RecordingLLM(None, finish_reason="content_filter")
    provider = LLMScoringProvider(llm_client=llm, fallback=heuristic_provider())
    outcome = provider.score(card())
    # Fallback = эвристика: score не обязан быть 0, но это НЕ LLM-скор.
    # Гарантия: не упало, вернуло числовой score + маркер эвристики.
    assert outcome.score_0_100 >= 0.0
    assert "employer_tier" in outcome.breakdown


def test_llm_provider_exception_falls_back_without_raising():
    provider = LLMScoringProvider(
        llm_client=_FailingLLM(ConnectionError("LLM недоступен")),
        fallback=heuristic_provider(),
    )
    outcome = provider.score(card())
    assert outcome.score_0_100 >= 0.0
    assert "employer_tier" in outcome.breakdown


def test_llm_provider_bad_json_falls_back_to_heuristic():
    provider = LLMScoringProvider(
        llm_client=_RecordingLLM("это не JSON"),
        fallback=heuristic_provider(),
    )
    outcome = provider.score(card())
    assert "employer_tier" in outcome.breakdown


def test_llm_provider_fallback_uses_tier_boost():
    # Даже при fallback известная компания получает буст employer_tier.
    provider = LLMScoringProvider(
        llm_client=_RecordingLLM(None),
        fallback=heuristic_provider(),
    )
    known = provider.score(card(company="Яндекс"))
    unknown = provider.score(card(company="ООО Ромашка"))
    assert known.breakdown["employer_tier"] > unknown.breakdown["employer_tier"]


# --- HeuristicScoringProvider: обёртка над #15 + tier-буст (#74 Этап 2) ------


def test_heuristic_provider_applies_tier_boost_for_known_company():
    provider = heuristic_provider(weights=ScoringWeights(text_match=0.0))
    known = provider.score(card(title="Python", company="Яндекс"))
    unknown = provider.score(card(title="Python", company="ООО Ромашка"))
    # Одинаковый title/filters → база #15 равна; разница только в employer_tier.
    assert known.score_0_100 > unknown.score_0_100
    assert known.breakdown["employer_tier"] > 0.0
    assert unknown.breakdown["employer_tier"] == 0.0
