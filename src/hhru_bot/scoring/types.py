"""Общие типы скоринга: ScoreOutcome, ScoringProvider (Protocol), EmployerInfo (#74)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile
    from ..search import VacancyCard


@dataclass
class EmployerInfo:
    """Распарсенная информация о работодателе из карточки поиска (Этап 1, #74).

    Все поля опциональны — hh.ru показывает рейтинг/trusted не для всех карточек
    (напр. у нового работодателя без отзывов). rating — средняя оценка 0-5;
    reviews_count — число отзывов; trusted — бейдж «надёжный работодатель».

    trusted на живой выдаче есть у ~98% карточек (#118, залогиненный дамп
    50 карточек: 49/50) — как сигнал качества непригоден, поэтому ни
    prefilter (employer_passes_prefilter), ни classify_employer его не
    используют. Поле оставлено: 1 карточка из 50 всё же отличается, может
    пригодиться будущим сигналам с малым весом.
    """

    rating: float | None = None
    reviews_count: int | None = None
    trusted: bool = False


@dataclass
class ScoreOutcome:
    """Результат скоринга одной вакансии (0-100 + rationale + разбивка).

    score_0_100 — итоговый скор В ДИАПАЗОНЕ [0, 100]. LLM отдаёт 0-100 напрямую;
    эвристика нормализует свой сырой score в [0, 100] монотонным clamp'ом (см.
    HeuristicScoringProvider), чтобы fallback и LLM-скор были на ОДНОЙ шкале —
    иначе при частичном сбое LLM таймаут на релевантной вакансии опускал бы её
    ниже посредственных LLM-успехов (Codex #74 F2). mode — источник скоринга
    ('heuristic' | 'llm') для логов/A/B и диагностики смешанных батчей. rationale
    — короткое текстовое объяснение. breakdown — факторы по имени.
    """

    score_0_100: float
    mode: str = "heuristic"
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
