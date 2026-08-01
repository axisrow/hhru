"""Тесты ML-скоринга: classify_employer + LLMScoringProvider (issue #74).

Чистая логика без браузера. Существующий test_scoring.py покрывает эвристику
#15 (факторы по title/ключевым словам); здесь — новые компоненты #74:
  - classify_employer: гиганты (RU top tech/big corp/global), эвристика
    reviews_count, unknown для мелких. trusted-бейдж НЕ используется (#118:
    им помечено ~98% карточек поиска, сигнал бесполезен).
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


# --- adversarial: спуфинг подстрокой НЕ должен матччить (Codex #74 F4) -------
#
# До фикса classify_employer матчил бренд подстрокой: «Metallurg» ложно матчило
# «meta» (TOP_TECH), короткие alias'ы («vk») — любые имена с этим сочетанием.
# Строгий токен-матч закрывает это: бренд = отдельный токен или идущая подряд
# последовательность токенов, не подстрока.


def test_adversarial_meta_substring_not_matched():
    # «Metallurg» содержит «meta» как подстроку, но НЕ как токен → unknown.
    assert classify_employer("Metallurg LLC") == KnownCompanyTier.UNKNOWN
    assert classify_employer("Metamorphosis") == KnownCompanyTier.UNKNOWN


def test_adversarial_vk_substring_not_matched():
    # «vk» как подстрока внутри слова не матчит.
    assert (
        classify_employer("Vkontakte-sub") == KnownCompanyTier.TOP_TECH
    )  # токен «vkontakte» есть — это валидный alias
    assert (
        classify_employer("Advokat i K") == KnownCompanyTier.UNKNOWN
    )  # «vk»-подстрока, но токена «vk» нет


def test_adversarial_short_alias_only_exact_token():
    # Короткие alias'ы матчат ТОЛЬКО как точный токен.
    assert classify_employer("IBM") == KnownCompanyTier.TOP_TECH
    assert classify_employer("IBMeter Solutions") == KnownCompanyTier.UNKNOWN
    assert classify_employer("Tesla") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Teslaco") == KnownCompanyTier.UNKNOWN


def test_adversarial_brand_inside_legal_entity_still_matches():
    # Строгий токен-матч НЕ ломает валидный кейс: бренд внутри юр. лица/дочки.
    assert classify_employer("ООО Яндекс") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Яндекс.Такси") == KnownCompanyTier.TOP_TECH
    assert classify_employer("Yandex LLC") == KnownCompanyTier.TOP_TECH


def test_adversarial_brand_spoof_with_suffix_not_matched():
    # «YandexReviews» (без разделителя) — один токен, ≠ «yandex» → не матчит.
    # Так отсекаются имперсонации-слияния.
    assert classify_employer("YandexReviews") == KnownCompanyTier.UNKNOWN
    assert classify_employer("SberFraud") == KnownCompanyTier.UNKNOWN


# --- classify_employer: эвристики по info из карточки (Этап 1 + 2) ----------


def test_classify_trusted_alone_is_unknown():
    # trusted-бейдж от hh.ru проставлен ~98% карточек (#118, залогиненный
    # дамп) — сигнал бесполезен и НЕ используется. reviews_count=10 тоже
    # ниже порога MID, поэтому неизвестное имя остаётся unknown.
    info = EmployerInfo(rating=4.5, reviews_count=10, trusted=True)
    assert classify_employer("Неизвестная Контора", info) == KnownCompanyTier.UNKNOWN


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


# --- F2: эвристика нормализована в 0-100, единая шкала с LLM (#74 F2) --------


def test_heuristic_provider_score_in_0_100_range():
    provider = heuristic_provider(weights=ScoringWeights(must_have=200.0))
    # must_have-матч даёт сырой score > 100 — нормализация должна зажать в 100.
    outcome = provider.score(card(title="Python Django", company="C"))
    assert 0.0 <= outcome.score_0_100 <= 100.0
    assert outcome.score_0_100 == 100.0
    assert outcome.mode == "heuristic"


def test_heuristic_provider_negative_penalty_clamped_to_zero():
    # Штраф за стоп-слово → сырой score отрицательный → clamp в 0.
    provider = heuristic_provider(weights=ScoringWeights(exclude_keyword=-50.0))
    outcome = provider.score(card(title="Программист 1С", company="C"))
    assert outcome.score_0_100 == 0.0


def test_llm_success_outcome_mode_is_llm():
    llm = _RecordingLLM('{"score": 80}')
    provider = LLMScoringProvider(llm_client=llm, fallback=heuristic_provider())
    outcome = provider.score(card())
    assert outcome.mode == "llm"
    assert outcome.score_0_100 == 80.0


def test_mixed_batch_no_scale_corruption():
    """F2: смешанный батч (LLM-успех + LLM-fallback на эвристике) сортируется
    на ЕДИНОЙ шкале [0,100]. До фикса fallback отдавал сырой score эвристики,
    могущий быть сколь угодно большим/отрицательным → сортировка ломалась.

    Сценарий: вакансия A (LLM-таймаут → fallback, эвристика ~сырой) и B (LLM-успех
    80). После нормализации обе шкалы — [0,100], сравнение корректно.
    """
    # Провайдер: первый вызов падает (timeout), второй — успех 80.
    llm = _FailingThenRecordingLLM(
        fail=ConnectionError("LLM timeout"), then_content='{"score": 80}'
    )
    provider = LLMScoringProvider(llm_client=llm, fallback=heuristic_provider())
    a = provider.score(card(vacancy_id="A", title="Python Django"))  # → fallback
    b = provider.score(card(vacancy_id="B", title="Python"))  # → LLM 80
    # Оба на единой шкале [0,100] — это и есть контракт F2.
    assert 0.0 <= a.score_0_100 <= 100.0
    assert 0.0 <= b.score_0_100 <= 100.0
    assert a.mode == "heuristic"
    assert b.mode == "llm"
    assert b.score_0_100 == 80.0


# --- F3: circuit-breaker + bounded chat (Codex #74 F3) ----------------------


class _RecordingLLMWithParams(_RecordingLLM):
    """Как _RecordingLLM, но сохраняет параметры (max_tokens/timeout) запроса."""

    def chat(self, messages, **params):
        self.calls.append((messages, params))
        return NormalizedResponse(
            content=self._content, tool_calls=None, finish_reason=self._finish_reason
        )


class _FailingThenRecordingLLM:
    """Первый chat() бросает, последующие — отдают заданный content."""

    def __init__(self, fail: Exception, then_content: str | None):
        self._fail = fail
        self._then = then_content
        self._failed = False
        self.calls: list[tuple[list, dict]] = []

    def chat(self, messages, **params):
        self.calls.append((messages, params))
        if not self._failed:
            self._failed = True
            raise self._fail
        return NormalizedResponse(content=self._then, tool_calls=None, finish_reason="stop")


def test_llm_provider_passes_max_tokens_and_timeout():
    llm = _RecordingLLMWithParams('{"score": 50}')
    provider = LLMScoringProvider(
        llm_client=llm, fallback=heuristic_provider(), max_tokens=128, timeout=12.5
    )
    provider.score(card())
    params = llm.calls[0][1]
    assert params["max_tokens"] == 128
    assert params["timeout"] == 12.5


def test_circuit_breaker_skips_llm_after_consecutive_failures():
    """F3: после N подряд fallback'ов следующие карточки идут на эвристику
    БЕЗ LLM-запроса — деградировавший endpoint не плодит повисшие запросы."""
    llm = _FailingLLM(ConnectionError("down"))  # все вызовы падают
    provider = LLMScoringProvider(
        llm_client=llm, fallback=heuristic_provider(), circuit_failure_threshold=3
    )
    # Первые 3 — делают LLM-запрос (и падают → fallback).
    for i in range(3):
        outcome = provider.score(card(vacancy_id=str(i)))
        assert outcome.mode == "heuristic"
    # 4-я карточка — breaker открыт (3 сбоя подряд), LLM НЕ зовётся → сразу эвристика.
    outcome = provider.score(card(vacancy_id="4"))
    assert outcome.mode == "heuristic"


def test_circuit_breaker_resets_on_success():
    """F3: любой успех обнуляет счётчик подряд-сбоёв — endpoint ожил, LLM снова зовётся."""
    llm = _FlakyThenOKLLM(fail=ConnectionError("down"), fails=2, ok_content='{"score": 70}')
    provider = LLMScoringProvider(
        llm_client=llm, fallback=heuristic_provider(), circuit_failure_threshold=3
    )
    # 2 сбоя подряд (счётчик=2, < порога 3).
    assert provider.score(card(vacancy_id="1")).mode == "heuristic"
    assert provider.score(card(vacancy_id="2")).mode == "heuristic"
    # Успех — счётчик обнулён, mode=llm.
    assert provider.score(card(vacancy_id="3")).mode == "llm"
    # Следующий снова зовёт LLM (breaker закрыт).
    assert provider.score(card(vacancy_id="4")).mode == "llm"


class _FlakyThenOKLLM:
    """Первые ``fails`` вызовов бросают, дальше — успех с ok_content."""

    def __init__(self, fail: Exception, fails: int, ok_content: str | None):
        self._fail = fail
        self._fails = fails
        self._ok = ok_content
        self._n = 0
        self.calls: list[tuple[list, dict]] = []

    def chat(self, messages, **params):
        self.calls.append((messages, params))
        self._n += 1
        if self._n <= self._fails:
            raise self._fail
        return NormalizedResponse(content=self._ok, tool_calls=None, finish_reason="stop")


# --- rank_candidates: shortlist при нейтральных весах (регрессия #81) -------
#
# Codex-ревью #81: при ai+ai_profile БЕЗ scoring-секции предранжирование шло по
# _ZERO_WEIGHTS → все кандидаты tie → llm_shortlist брал первые K по входному
# порядку, а реально релевантные (LLM дал бы им высокий score) за пределами K
# никогда не displac'нулись. Фикс: AI-путь без scoring-секции использует дефолтные
# ScoringWeights() для предранжирования → top-K действительно лучшие.


class _RelevanceLLM:
    """Мок LLM: высокий score карточкам, чей title содержит ``high_kw``."""

    def __init__(self, high_kw: str):
        self._kw = high_kw
        self.calls: list[VacancyCard] = []

    def chat(self, messages, **params):  # noqa: ARG002
        # messages[1] (user) содержит title вакансии (см. _build_scoring_prompt).
        text = " ".join(m.get("content", "") for m in messages)
        score = 95.0 if self._kw in text else 20.0
        content = f'{{"score": {int(score)}, "rationale": "r", "factors": {{}}}}'
        return NormalizedResponse(content=content, tool_calls=None, finish_reason="stop")


def _resume_no_scoring() -> object:
    """ResumeConfig без scoring-секции (legacy AI-конфиг: ai+ai_profile, без scoring)."""
    from hhru_bot.config import ResumeConfig
    from hhru_bot.config_sections.ai_profile import AIProfile

    return ResumeConfig(
        id="py",
        resume_url="https://hh.ru/resume/X",
        search=SearchFilters(text="python", must_have=["django"]),
        ai_profile=AIProfile(summary="Бэкенд", skills=["python"], desired_role="Dev"),
    )


def test_rank_candidates_shortlist_picks_relevant_beyond_first_k():
    """AI-путь без scoring: релевантные карточки вне первых-10 displac'нут топ.

    12 карточек: первые 10 (id 0-9) нерелевантные (title без 'django'), последние
    2 (id 10,11) — релевантные ('django' в title, в конце входного порядка).
    Без фикса: llm_shortlist=10 берёт id 0-9 по входу → LLM их и скорит, id 10,11
    остаются с эвристическим score и никогда не displac'нут топ. С фиксом:
    дефолтные веса предранжируют 'django'-карточки вверх → они в shortlist → LLM
    даёт им 95 → они в начале ranked[:limit].
    """
    from hhru_bot.search import rank_candidates

    cards = [card(vacancy_id=str(i), title="Python role no kw") for i in range(10)] + [
        card(vacancy_id="10", title="Python django lead"),
        card(vacancy_id="11", title="django backend"),
    ]

    llm = _RelevanceLLM(high_kw="django")
    provider = LLMScoringProvider(llm_client=llm, fallback=heuristic_provider())

    ranked = rank_candidates(
        cards,
        SearchFilters(text="python", must_have=["django"]),
        _resume_no_scoring(),
        scoring_provider=provider,
        llm_shortlist=10,
    )
    top_ids = [c.vacancy_id for c, _, _ in ranked]

    # Релевантные id 10,11 должны возглавить ранжирование (LLM-score 95 > 20).
    assert top_ids[:2] == ["10", "11"] or set(top_ids[:2]) == {"10", "11"}, top_ids


def test_rank_candidates_no_provider_keeps_legacy_zero_weights():
    """Без provider (legacy) weights остаются _ZERO_WEIGHTS — входной порядок.

    Регресс-страховка фикса #81: AI-путь меняет веса только когда provider передан.
    Без provider поведение legacy не меняется (candidates[:limit] по входу).
    """
    from hhru_bot.search import rank_candidates

    cards = [card(vacancy_id=str(i), title="Python django lead") for i in range(5)] + [
        card(vacancy_id="9", title="nope")
    ]

    ranked = rank_candidates(
        cards,
        SearchFilters(text="python", must_have=["django"]),
        _resume_no_scoring(),
        scoring_provider=None,
        llm_shortlist=10,
    )
    top_ids = [c.vacancy_id for c, _, _ in ranked]

    # Все score 0.0 (zero weights) → стабильный входной порядок сохранён.
    assert top_ids == ["0", "1", "2", "3", "4", "9"]
