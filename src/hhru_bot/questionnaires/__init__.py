"""Обучаемые шаблоны ответов на анкеты работодателей (#482).

Пакет намеренно не зависит от Playwright и не импортирует LLM-слой на верхнем
уровне: цепочка `ключевые слова -> (будущий ML-слот) -> LLM -> вопрос
пользователю` начинается с полностью детерминированного keyword resolver'а,
который обязан работать при отсутствующей optional-зависимости ``.[ai]``
(критерий приёмки #482: «Keyword resolver работает без AI-зависимости»).

Разделение ответственностей:
  * ``templates`` — доменная модель шаблона и seed-поля, чистые данные;
  * ``resolver``  — стратегии сопоставления вопрос->шаблон и построение ответа;
  * ``answerer``  — composite, который связывает их с историей и pipeline.

``answerer`` здесь НЕ реэкспортируется: он тянет ``ai.questions`` (ради
переиспользования ``AnswerProposal``/``AIQuestionAnswerer.apply``), а этот
пакет должен оставаться импортируемым для чистой логики и CLI без AI.
"""

from __future__ import annotations

from .resolver import (
    ResolvedAnswer,
    TemplateMatch,
    build_answer,
    check_choice_compatibility,
    choice_indices,
    compliance_gate,
    match_keyword,
    match_phrase,
    resolve_template,
)
from .templates import (
    CLUSTERS,
    DEFAULT_CLUSTER,
    SEED_TEMPLATES,
    STRICT_CLUSTERS,
    QuestionTemplate,
    SeedTemplate,
    cluster_for,
    is_strict,
    seed_template,
)

__all__ = [
    "CLUSTERS",
    "DEFAULT_CLUSTER",
    "SEED_TEMPLATES",
    "STRICT_CLUSTERS",
    "QuestionTemplate",
    "ResolvedAnswer",
    "SeedTemplate",
    "TemplateMatch",
    "build_answer",
    "check_choice_compatibility",
    "choice_indices",
    "cluster_for",
    "compliance_gate",
    "is_strict",
    "match_keyword",
    "match_phrase",
    "resolve_template",
    "seed_template",
]
