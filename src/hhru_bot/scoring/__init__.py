"""ML-скоринг вакансий простым путём: классификатор работодателя + LLMScoringProvider (#74).

Пакет — фундамент для семейства видов скоринга (#491): классификатор известности
работодателя (``classify_employer``, tier-буст) + ``ScoringProvider``/
``HeuristicScoringProvider``/``LLMScoringProvider`` (релевантность вакансии
профилю кандидата, #74) + pre-LLM фильтр работодателя (``employer_passes_prefilter``,
#85). Раскладка по модулям: ``types`` (общие типы), ``employer`` (классификатор
известности), ``prefilter`` (pre-LLM фильтр), ``vacancy`` (эвристика + LLM
провайдер), ``resume_match`` (keyword-соответствие профиля кандидата вакансии,
#492). Этот файл — публичный API пакета (#491): ре-экспортирует всё, что
было публичным в дореформенном ``scoring.py``, без изменений. Приватные
хелперы (``_parse_llm_score``, ``_build_scoring_prompt`` и т.п.) остаются
деталями своих модулей и сюда не поднимаются — тесты, которым они нужны
напрямую, импортируют их из ``scoring.vacancy``.
"""

from __future__ import annotations

from .employer import TIER_BOOST, KnownCompanyTier, classify_employer
from .prefilter import PREFILTER_SKIP_REASON, employer_passes_prefilter
from .resume_match import RESUME_MATCH_MODE, resume_match_score
from .types import EmployerInfo, ScoreOutcome, ScoringProvider
from .vacancy import HeuristicScoringProvider, LLMScoringProvider, heuristic_score

__all__ = [
    "PREFILTER_SKIP_REASON",
    "RESUME_MATCH_MODE",
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
    "resume_match_score",
]
