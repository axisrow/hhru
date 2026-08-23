"""Эвристический провайдер + LLMScoringProvider (Этап 3, #74).

Связывает classify_employer (#74 Этап 2) с эвристикой #15: поверх взвешенной
суммы факторов из ``search._score_card`` накладывается буст за tier компании.
Это fallback для LLMScoringProvider и дефолт для ``rank_candidates`` (когда
провайдер не передан) — но ``rank_candidates`` продолжает вызывать ``_score_card``
напрямую для чистоты обратной совместимости; этот провайдер нужен LLM-провайдеру
как точка отката и может использоваться тестами/явно.

LLM (#16 LLMClient) оценивает релевантность карточки профилю кандидата, возвращая
score 0-100 + rationale. Любой сбой (None-контент, плохой JSON, исключение,
нет openai) → молчаливый fallback на эвристику БЕЗ исключения. Паттерн — как
``CoverLetterProvider``/``AICoverLetterProvider`` (#17): abstraction +
AI-реализация поверх LLMClient + устойчивый fallback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from .employer import TIER_BOOST, classify_employer
from .types import ScoreOutcome

if TYPE_CHECKING:
    from ..ai import LLMClient
    from ..config_sections.ai_profile import AIProfile
    from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.scoring")


# Диапазон нормализации эвристического score (Codex #74 F2). Эвристика #15 —
# взвешенная сумма факторов, может быть отрицательной (штраф за стоп-слово) или
# большой (много must_have-хитов + tier-буст). Монотонный clamp в [0, 100]:
# сохраняет ОТНОСИТЕЛЬНЫЙ порядок карточек эвристики (важно для ранжирования) и
# приводит шкалу к LLM-диапазону, чтобы смешанный батч сортировался корректно.
_HEURISTIC_SCORE_FLOOR = 0.0
_HEURISTIC_SCORE_CEIL = 100.0


def _normalize_heuristic_score(raw: float) -> float:
    """Монотонно отображает сырой эвристический score в [0, 100].

    Clamp (не сигмоид): проще, детерминированно, сохраняет порядок эвристики.
    Карточки с raw >= 100 склеиваются в 100 (теряется разрешение только при
    больших пересечениях стека — редкий случай, приемлемо для v1).
    """
    if raw < _HEURISTIC_SCORE_FLOOR:
        return _HEURISTIC_SCORE_FLOOR
    if raw > _HEURISTIC_SCORE_CEIL:
        return _HEURISTIC_SCORE_CEIL
    return raw


def heuristic_score(
    card: VacancyCard,
    filters,
    weights,
) -> tuple[float, dict[str, float]]:
    """Эвристика #15 + tier-буст работодателя (#74 Этап 2).

    Возвращает (score, breakdown). breakdown содержит факторы #15 И отдельный
    ключ ``employer_tier`` с величиной буста (для прозрачности/логов). Импортирует
    ``_score_card`` лениво, чтобы избежать цикла ``search`` <-> ``scoring``.
    """
    from ..search import _score_card  # локальный импорт: разрыв цикла

    base, breakdown = _score_card(card, filters, weights)
    tier = classify_employer(card.company, getattr(card, "employer_info", None))
    boost = TIER_BOOST.get(tier, 0.0)
    breakdown = {**breakdown, "employer_tier": boost}
    return base + boost, breakdown


class HeuristicScoringProvider:
    """Провайдер, реализующий интерфейс ScoringProvider поверх эвристики #15.

    Нужен как явный «дефолтный» провайдер и как fallback в LLMScoringProvider.
    Хранит filters/weights (из резюме), чтобы ``score`` соответствовал сигнатуре
    протокола (card, resume_profile) и не требовал передачи фильтров каждый раз.
    """

    def __init__(self, filters, weights):
        self._filters = filters
        self._weights = weights

    def score(self, card: VacancyCard, resume_profile=None) -> ScoreOutcome:  # noqa: ARG002
        raw, breakdown = heuristic_score(card, self._filters, self._weights)
        # Нормализация в [0, 100]: шкала эвристики приводится к LLM-диапазону,
        # чтобы смешанный батч (LLM-успех + fallback) сортировался корректно
        # (Codex #74 F2). mode='heuristic' — маркер источника для логов/A/B.
        return ScoreOutcome(
            score_0_100=_normalize_heuristic_score(raw),
            mode="heuristic",
            breakdown=breakdown,
        )


# Ожидаемая структура JSON-ответа от LLM: {"score": 0-100, "rationale": "...",
# "factors": {...}}. factors опциональны — главное поле score. Рационально
# ограничиваем длину, чтобы не тащить простыню текста в breakdown/логи.
_MAX_RATIONALE_LEN = 240

# Границы LLM-запроса (Codex #74 F3). JSON-ответ скоринга короткий → 256 токенов
# с запасом; жёсткий timeout, чтобы деградировавший endpoint не держал автоматику
# hh.ru бесконечно (принцип «не выглядеть подозрительно» — меньше зависших
# сессий). LLMClient.forward'ит **params в transport (temperature/max_tokens/timeout).
_LLM_MAX_TOKENS = 256
_LLM_TIMEOUT = 30.0

# Circuit breaker: сколько ПОДРЯД fallback'ов терпим, прежде чем решить, что
# endpoint деградировал, и перестать звать LLM до конца батча (Codex #74 F3).
# Любой успех обнуляет счётчик (endpoint ожил). Без этого деградация грозила
# десятками повисших синхронных запросов к hh.ru/LLM.
_LLM_CIRCUIT_FAILURE_THRESHOLD = 3


class LLMScoringProvider:
    """Скоринг вакансии через LLM (#16), с устойчивым fallback на эвристику (#74).

    llm_client — готовый ``LLMClient`` (построен в commands из AppConfig.ai).
    fallback — ``HeuristicScoringProvider`` (эвристика #15 + tier-буст), на
    который откатываемся при любом сбое AI: исключение, None-контент, пустой
    текст, невалидный JSON, score вне [0,100]. resume_profile — данные кандидата
    для релевантного промпта; None → скоринг по вакансии без профиля.

    Главный инвариант: ``score`` НИКОГДА не бросает — сбой LLM не должен валить
    ранжирование (иначе автоматика hh.ru теряет лучшие совпадения из-за временной
    недоступности LLM). Точно как AICoverLetterProvider в #17.

    Circuit breaker (Codex #74 F3): после ``circuit_failure_threshold`` ПОДРЯД
    fallback'ов подряд следующие карточки сразу уходят на эвристику, не делая
    LLM-запрос — деградировавший endpoint не тратит время/деньги и не плодит
    подозрительную нагрузку. Любой успех обнуляет счётчик. Жёсткий timeout +
    max_tokens ограничивают каждый запрос. stateful — счётчик живёт один батч
    (один вызов rank_candidates).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        fallback: HeuristicScoringProvider,
        resume_profile: AIProfile | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = _LLM_MAX_TOKENS,
        timeout: float = _LLM_TIMEOUT,
        circuit_failure_threshold: int = _LLM_CIRCUIT_FAILURE_THRESHOLD,
    ):
        self._llm = llm_client
        self._fallback = fallback
        self._profile = resume_profile
        # Низкая temperature: скоринг должен быть детерминированным/оценочным,
        # а не «творческим» (как письмо). Меньше разброса между одинаковыми
        # вакансиями → стабильнее ранжирование.
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._circuit_failure_threshold = circuit_failure_threshold
        # Счётчик подряд идущих fallback'ов (circuit breaker). Обнуляется на успехе.
        self._consecutive_failures = 0

    def score(self, card: VacancyCard, resume_profile=None) -> ScoreOutcome:
        profile = resume_profile or self._profile

        # Circuit breaker открыт: endpoint деградировал, не делаем запрос —
        # сразу эвристика (без наращивания счётчика: мы LLM не звали).
        if self._consecutive_failures >= self._circuit_failure_threshold:
            logger.info(
                "LLM scoring circuit open for '%s' — heuristic fallback (no LLM call)",
                card.title,
            )
            return self._fallback.score(card, profile)

        messages = _build_scoring_prompt(card, profile)
        try:
            response = self._llm.chat(
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 — широкий: любой сбой AI → fallback
            logger.warning(
                "LLM scoring failed for '%s': %s — fallback to heuristic",
                card.title,
                e,
            )
            return self._fallback_for_failure(card, profile)

        content = response.content if response is not None else None
        outcome = _parse_llm_score(content, card)
        if outcome is None:
            logger.warning(
                "LLM returned unusable score for '%s' (finish_reason=%s) — fallback to heuristic",
                card.title,
                getattr(response, "finish_reason", "?"),
            )
            return self._fallback_for_failure(card, profile)

        # Успех: обнуляем счётчик подряд-сбоев (endpoint ожил).
        self._consecutive_failures = 0
        return outcome

    def _fallback_for_failure(self, card: VacancyCard, profile) -> ScoreOutcome:
        """Фиксирует подряд-сбой в счётчике breaker'а и отдаёт эвристику."""
        self._consecutive_failures += 1
        return self._fallback.score(card, profile)


def _parse_llm_score(content: str | None, card: VacancyCard) -> ScoreOutcome | None:
    """Разбирает JSON-ответ LLM в ScoreOutcome, либо None (→ fallback).

    Принимает «грязный» контент: LLM может обернуть JSON в markdown-блок
    ```json ... ``` или добавить пояснения. Извлекаем первый JSON-объект
    regex'ом и парсим. None/пусто/не-JSON/score вне [0,100] → None (точка
    fallback, НЕ исключение — см. инвариант LLMScoringProvider).
    """
    if not content or not content.strip():
        return None

    text = content.strip()
    # Снимаем markdown-обёртку ```json ... ``` (LLM часто добавляет).
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    raw_score = data.get("score")
    if raw_score is None:
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None

    # Score обязан быть в [0, 100]; иначе считаем ответ невалидным → fallback.
    if not 0.0 <= score <= 100.0:
        return None

    rationale = str(data.get("rationale", "") or "").strip()[:_MAX_RATIONALE_LEN]

    factors = data.get("factors")
    breakdown: dict[str, float] = {}
    if isinstance(factors, dict):
        # Берём только числовые факторы, отсекая мусор; имена факторов — ключи.
        for key, value in factors.items():
            try:
                breakdown[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    return ScoreOutcome(score_0_100=score, mode="llm", rationale=rationale, breakdown=breakdown)


def _build_scoring_prompt(card: VacancyCard, profile: AIProfile | None) -> list[dict[str, str]]:
    """Собирает chat-completion сообщения для скоринга вакансии под кандидата.

    Контекст — карточка поиска (title/company/salary + employer_info: рейтинг/
    tier) и профиль кандидата (skills/role/исключения). Просим вернуть СТРОГО
    JSON без markdown/пояснений, чтобы ``_parse_llm_score`` отработал.

    ``info.trusted`` в промпт НЕ попадает: тот же дефект, что #118 чинит в
    prefilter (~98% карточек несут этот бейдж, LLM видел бы одинаковую
    «похвалу» почти на каждой вакансии и смещал бы score без реальной
    информации — только за токены).
    """
    system = (
        "Ты оцениваешь релевантность вакансии кандидату. Оценивай только по "
        "данным из карточки и профиля, без выдумок. Верни ТОЛЬКО валидный JSON "
        "без markdown и пояснений вида "
        '{"score": <0-100>, "rationale": "<коротко>", "factors": {}}. '
        "score — насколько вакансия подходит кандидату (0-100). "
        "rationale — одна-две фразы по-русски. factors — числовые факторы."
    )

    lines = [f"Вакансия: {card.title}.", f"Компания: {card.company or 'не указана'}."]
    salary = card.salary
    if salary is not None:
        lines.append(f"Зарплата: {salary.raw}.")
    info = getattr(card, "employer_info", None)
    if info is not None:
        tier = classify_employer(card.company, info)
        parts = [f"tier работодателя: {tier}"]
        if info.rating is not None:
            parts.append(f"рейтинг: {info.rating}")
        if info.reviews_count is not None:
            parts.append(f"отзывов: {info.reviews_count}")
        lines.append("Работодатель: " + ", ".join(parts) + ".")
    # VacancyCard.__post_init__ (search.py) already populates this from
    # vacancy_text whenever it is set, so no fallback recomputation is needed
    # here — a card with vacancy_text always has a non-None requirement.
    requirement = getattr(card, "portfolio_evidence_requirement", None)
    if requirement is not None and requirement.level != "none":
        lines.append(
            "Требование портфолио/проектов: "
            f"{requirement.level}; источник: " + " | ".join(requirement.evidence)
        )

    if profile is not None:
        if profile.summary:
            lines.append(f"О кандидате: {profile.summary}.")
        if profile.skills:
            lines.append("Ключевые навыки: " + ", ".join(profile.skills) + ".")
        if profile.desired_role:
            lines.append(f"Желаемая роль: {profile.desired_role}.")

    lines.append("Оцени релевантность этой вакансии кандидату и верни JSON.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]
