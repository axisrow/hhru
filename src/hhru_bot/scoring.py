"""ML-скоринг вакансий простым путём: классификатор работодателя + LLMScoringProvider (#74).

Расширение #15 (эвристика по title/ключевым словам) поверх двух новых факторов:
  1. **classify_employer** — «известная/неизвестная компания» как фактор скоринга
     (буст за Яндекс/Сбер/FAANG и пр.). БЕЗ ML: O(1) lookup по встроенному списку
     гигантов + эвристики по trusted/reviews_count из карточки поиска hh.ru.
  2. **ScoringProvider / LLMScoringProvider** — «ML простым путём»: LLM (#16
     LLMClient) оценивает релевантность карточки профилю кандидата, возвращая
     score 0-100 + rationale. Любой сбой (None-контент, плохой JSON, исключение,
     нет openai) → молчаливый fallback на эвристику ``search._score_card`` БЕЗ
     исключения. Паттерн — как ``CoverLetterProvider``/``AICoverLetterProvider``
     (#17): abstraction + AI-реализация поверх LLMClient + устойчивый fallback.

Принципы проекта соблюдены: никакого обучения моделей/датасетов/deepagents/внешних
сайтов/новостей — только hh.ru (карточка) + LLM. Опциональность: без секции ``ai``
в конфиге провайдер не строится, ранжирование идёт по эвристике (#15).
``openai`` не нужен для импорта этого модуля — LLMClient лениво тянет его только
в момент ``chat()`` (см. ``hhru_bot.ai.letters``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .ai import LLMClient
    from .config_sections.ai_profile import AIProfile
    from .search import VacancyCard

logger = logging.getLogger("hhru_bot.scoring")


# --- классификатор «известная/неизвестная компания» (Этап 2) ------------------
#
# Чистая функция без ML. Два источника решения:
#   1. O(1) lookup по встроенному списку гигантов (RU big tech + банки + global
#      FAANG/BigTech). Список намеренно короткий и правится руками — никаких
#      парсингов википедии/новостей (принцип простоты).
#   2. Эвристики по данным карточки hh.ru: trusted=True → известная;
#      reviews_count >= порога → известная (много отзывов = крупная компания).


class KnownCompanyTier:
    """Уровень «известности» работодателя (строковые константы, не Enum — ради простоты).

    Порядок «известности»: top_tech > big_corp > mid > unknown. Буст в скоринге
    зависит от tier (см. TIER_BOOST). unknown = ничего не знаем о компании.
    """

    TOP_TECH = "top_tech"  # Яндекс/Сбер/VK/FAANG — буст максимальный
    BIG_CORP = "big_corp"  # МТС/Альфа/Авито и пр. крупные — буст средний
    MID = "mid"  # средний бизнес (много отзывов, но не в списке) — слабый буст
    UNKNOWN = "unknown"  # ООО «Ромашка» без отзывов — буста нет


# Гиганты RU: top tech + крупные банки/ритейл. Ключ — нижний регистр, проверка
# подстрокой (чтобы «ООО Яндекс»/«Яндекс.Такси»/«Yandex» матчило). Бренды, а не
# юр. лица: пользователь видит на hh.ru именно бренд.
_KNOWN_TOP_TECH_RU = (
    "яндекс",
    "yandex",
    "сбербанк",
    "сбер",
    "sber",
    "vk",
    "вконтакте",
    "ozon",
    "озон",
    "т-банк",
    "тинькофф",
    "tinkoff",
    "авито",
    "avito",
    "wildberries",
    "headhunter",
    "hh.ru",
    "газпромбанк",
    "российские железные дороги",
    "ржд",
)

# Крупный бизнес второй линии (банки/телеком/ритейл): буст ниже top tech, но
# выше неизвестной компании.
_KNOWN_BIG_CORP_RU = (
    "мтс",
    "mts",
    "альфа-банк",
    "alfabank",
    "альфа",
    "втб",
    "vtb",
    "мегамаркет",
    "ламода",
    "ламода",
    "ручная",
    "озон тех",
    "контур",
    "т-банк",
    "билайн",
    "beeline",
    "мегафон",
    "megafon",
    "ростелеком",
    "rtk",
    "ozon tech",
)

# Глобальные гиганты (FAANG/BigTech): высокий буст, как у RU top tech.
_KNOWN_GLOBAL = (
    "google",
    "гугл",
    "amazon",
    "амазон",
    "apple",
    "эпл",
    "microsoft",
    "майкрософт",
    "meta",
    "facebook",
    "netflix",
    "tesla",
    "nvidia",
    "openai",
    "ibm",
    "intel",
    "oracle",
    "salesforce",
)

# Порог reviews_count для эвристики MID: компания с таким количеством отзывов на
# hh.ru — заметный игрок рынка (но не обязательно из списка гигантов). Значение
# консервативное, чтобы не завышать tier мелким employer'ам.
_REVIEWS_COUNT_MID_THRESHOLD = 100


def _matches_any(name_lower: str, candidates: tuple[str, ...]) -> bool:
    """Проверяет, содержит ли name_lower любую из подстрок candidates."""
    return any(c in name_lower for c in candidates)


def classify_employer(name: str | None, info: EmployerInfo | None = None) -> str:
    """Классифицирует работодателя по уровню известности (Этап 2, #74).

    Чистая O(1) функция без ML. Возвращает одну из констант ``KnownCompanyTier``.

    Логика (по убыванию приоритета):
      1. Имя бренда в списке RU top tech ИЛИ глобальных гигантов → ``top_tech``.
      2. Имя бренда в списке RU big corp → ``big_corp``.
      3. ``info.trusted`` (надёжный работодатель по hh.ru) → ``big_corp``:
         hh.ru присваивает бейдж крупным/проверенным, это надёжный сигнал.
      4. ``info.reviews_count`` >= порога → ``mid`` (много отзывов, но не гигант).
      5. Иначе → ``unknown``.

    ``info`` опционален: без него работает только lookup по имени. Регистр
    игнорируется; матчится подстрока (бренд внутри юр. лица/дочки).
    """
    name_lower = (name or "").lower()

    if name_lower and (
        _matches_any(name_lower, _KNOWN_TOP_TECH_RU) or _matches_any(name_lower, _KNOWN_GLOBAL)
    ):
        return KnownCompanyTier.TOP_TECH

    if name_lower and _matches_any(name_lower, _KNOWN_BIG_CORP_RU):
        return KnownCompanyTier.BIG_CORP

    if info is not None:
        if info.trusted:
            return KnownCompanyTier.BIG_CORP
        if info.reviews_count is not None and info.reviews_count >= _REVIEWS_COUNT_MID_THRESHOLD:
            return KnownCompanyTier.MID

    return KnownCompanyTier.UNKNOWN


# Буст к эвристическому score за tier работодателя (#74 Этап 2). Накладывается
# поверх факторов #15 (must_have/nice_to_have/text_match/exclude_keyword), чтобы
# известная компания поднимала вакансию в ранжировании, но не перекрывала
# релевантность полностью. top_tech — заметный буст; unknown — ноль (нейтрально).
TIER_BOOST: dict[str, float] = {
    KnownCompanyTier.TOP_TECH: 4.0,
    KnownCompanyTier.BIG_CORP: 2.0,
    KnownCompanyTier.MID: 1.0,
    KnownCompanyTier.UNKNOWN: 0.0,
}


@dataclass
class EmployerInfo:
    """Распарсенная информация о работодателе из карточки поиска (Этап 1, #74).

    Все поля опциональны — hh.ru показывает рейтинг/trusted не для всех карточек
    (напр. у нового работодателя без отзывов). rating — средняя оценка 0-5;
    reviews_count — число отзывов; trusted — бейдж «надёжный работодатель».
    """

    rating: float | None = None
    reviews_count: int | None = None
    trusted: bool = False


# --- ScoringProvider: abstraction + AI-реализация (Этап 3) -------------------


@dataclass
class ScoreOutcome:
    """Результат скоринга одной вакансии (0-100 + rationale + разбивка).

    score_0_100 — итоговый скор в диапазоне [0, 100] (нормализованный, для LLM;
    эвристика отдаёт свой сырой score как есть, без перекладки в 0-100 — её
    ранжирование и так корректно по относительным величинам). rationale —
    короткое текстовое объяснение (для логов/A/B). breakdown — факторы по имени.
    """

    score_0_100: float
    rationale: str = ""
    breakdown: dict[str, float] = field(default_factory=dict)


class ScoringProvider(Protocol):
    """Абстракция провайдера скоринга вакансий (#74).

    ``score`` вызывается на каждую карточку в ``rank_candidates``. Провайдер
    обязан вернуть ``ScoreOutcome`` и НЕ бросать исключение: любой сбой (нет
    AI, None-контент, плохой JSON) обрабатывается внутри через fallback на
    эвристику. Так ранжирование никогда не падает целиком из-за временной
    недоступности LLM — критично для автоматики hh.ru (как и с письмами в #17).
    """

    def score(
        self,
        card: VacancyCard,
        resume_profile: AIProfile | None = None,
    ) -> ScoreOutcome: ...


# --- эвристический провайдер (обёртка над #15 + tier-буст) -------------------
#
# Связывает classify_employer (#74 Этап 2) с эвристикой #15: поверх взвешенной
# суммы факторов из ``search._score_card`` накладывается буст за tier компании.
# Это fallback для LLMScoringProvider и дефолт для ``rank_candidates`` (когда
# провайдер не передан) — но ``rank_candidates`` продолжает вызывать ``_score_card``
# напрямую для чистоты обратной совместимости; этот провайдер нужен LLM-провайдеру
# как точка отката и может использоваться тестами/явно.


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
    from .search import _score_card  # локальный импорт: разрыв цикла

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
        return ScoreOutcome(score_0_100=raw, breakdown=breakdown)


# --- LLMScoringProvider (#74 Этап 3) -----------------------------------------


# Ожидаемая структура JSON-ответа от LLM: {"score": 0-100, "rationale": "...",
# "factors": {...}}. factors опциональны — главное поле score. Рационально
# ограничиваем длину, чтобы не тащить простыню текста в breakdown/логи.
_MAX_RATIONALE_LEN = 240


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
    """

    def __init__(
        self,
        llm_client: LLMClient,
        fallback: HeuristicScoringProvider,
        resume_profile: AIProfile | None = None,
        *,
        temperature: float = 0.3,
    ):
        self._llm = llm_client
        self._fallback = fallback
        self._profile = resume_profile
        # Низкая temperature: скоринг должен быть детерминированным/оценочным,
        # а не «творческим» (как письмо). Меньше разброса между одинаковыми
        # вакансиями → стабильнее ранжирование.
        self._temperature = temperature

    def score(self, card: VacancyCard, resume_profile=None) -> ScoreOutcome:
        profile = resume_profile or self._profile
        messages = _build_scoring_prompt(card, profile)
        try:
            response = self._llm.chat(messages, temperature=self._temperature)
        except Exception as e:  # noqa: BLE001 — широкий: любой сбой AI → fallback
            logger.warning(
                "LLM scoring failed for '%s': %s — fallback to heuristic",
                card.title,
                e,
            )
            return self._fallback.score(card, profile)

        content = response.content if response is not None else None
        outcome = _parse_llm_score(content, card)
        if outcome is None:
            logger.warning(
                "LLM returned unusable score for '%s' (finish_reason=%s) — fallback to heuristic",
                card.title,
                getattr(response, "finish_reason", "?"),
            )
            return self._fallback.score(card, profile)

        return outcome


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

    return ScoreOutcome(score_0_100=score, rationale=rationale, breakdown=breakdown)


def _build_scoring_prompt(card: VacancyCard, profile: AIProfile | None) -> list[dict[str, str]]:
    """Собирает chat-completion сообщения для скоринга вакансии под кандидата.

    Контекст — карточка поиска (title/company/salary + employer_info: рейтинг/
    trusted/tier) и профиль кандидата (skills/role/исключения). Просим вернуть
    СТРОГО JSON без markdown/пояснений, чтобы ``_parse_llm_score`` отработал.
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
        if info.trusted:
            parts.append("надёжный работодатель")
        lines.append("Работодатель: " + ", ".join(parts) + ".")

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
