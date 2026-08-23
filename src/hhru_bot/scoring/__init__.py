"""Пакет скоринга: типы + классификатор работодателя + pre-LLM фильтр + скоринг вакансии.

Публичный API переэкспортирован здесь для обратной совместимости —
`from hhru_bot.scoring import classify_employer` работает как прежде.
Внутренние модули: `types.py` (общие ScoreOutcome/ScoringProvider/EmployerInfo),
`employer.py` (tier работодателя #74), `prefilter.py` (pre-LLM отсев #85),
`vacancy.py` (релевантность вакансии: эвристика #15 + LLM #74).

Новый вид скоринга (resume-match, letter-match и др.) = новый модуль рядом +
ре-экспорт здесь, а не top-level файл рядом с пакетом. Конфиг-секция скоринга
живёт отдельно — в `config_sections/scoring.py`.
"""

from __future__ import annotations

from .employer import TIER_BOOST, KnownCompanyTier, classify_employer
from .prefilter import PREFILTER_SKIP_REASON, employer_passes_prefilter
from .types import EmployerInfo, ScoreOutcome, ScoringProvider
from .vacancy import HeuristicScoringProvider, LLMScoringProvider, heuristic_score

__all__ = [
    "PREFILTER_SKIP_REASON",
    "TIER_BOOST",
    "EmployerInfo",
    "HeuristicScoringProvider",
    "KnownCompanyTier",
    "LLMScoringProvider",
    "ScoreOutcome",
    "ScoringProvider",
    "classify_employer",
    "employer_passes_prefilter",
    "heuristic_score",
]
