"""Keyword-скоринг соответствия сопроводительного письма вакансии (#493).

Эта функция намеренно переиспользует токенизацию, стем-матч и обработку
отрицаний из ``resume_match``. Письмо — единственный источник токенов
кандидата; в отличие от resume-match, здесь не нужен ``AIProfile`` и нет
взвешенных факторов.

Этап 1 — только вычисление и логирование score, без порога и без блокировки
отправки. Как и resume-match, результат использует общую шкалу
``ScoreOutcome.score_0_100``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .resume_match import NO_DATA_RATIONALE, _matched_ratio, _tokenize
from .types import ScoreOutcome

if TYPE_CHECKING:
    from ..search import VacancyCard


LETTER_MATCH_MODE = "letter_match"


def letter_match_score(card: VacancyCard, letter: str | None) -> ScoreOutcome:
    """Оценивает долю токенов письма, подтверждённых текстом вакансии.

    Пустое письмо или пустой ``vacancy_text`` дают ``0.0`` с отдельной
    пометкой «нет данных», а не считаются полным несовпадением. Это позволяет
    не смешивать в наблюдаемом распределении реальные нулевые матчи с
    карточками, для которых сопоставление было невозможно.
    """
    vacancy_tokens = _tokenize(getattr(card, "vacancy_text", "") or "")
    letter_text = letter or ""
    if not vacancy_tokens or not _tokenize(letter_text):
        return ScoreOutcome(
            score_0_100=0.0,
            mode=LETTER_MATCH_MODE,
            rationale=NO_DATA_RATIONALE,
            breakdown={},
        )

    score = round(_matched_ratio(letter_text, vacancy_tokens) * 100.0, 2)
    rationale = "keyword-match письма" if score > 0.0 else "совпадений нет"
    return ScoreOutcome(
        score_0_100=score,
        mode=LETTER_MATCH_MODE,
        rationale=rationale,
        breakdown={"letter": score},
    )
