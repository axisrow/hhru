"""Скоринг соответствия профиля резюме тексту вакансии (Этап 1, #492).

Чистая функция без ML: взвешенное пересечение токенов ``AIProfile``
(``skills``/``desired_role``/``highlights``/``summary``) с ``VacancyCard.vacancy_text``.
Источники текста подтверждены разведкой (#492): ``vacancy_text`` заполняется уже
на search-шаге (``search.py``, ``card.inner_text()``), полный текст резюме бот
нигде не парсит — скоринг работает по ``AIProfile``, не по резюме на hh.ru.

Этап 1 (эта issue): только вычисление и логирование score — без фильтрации,
без блокировки отправки (см. вызов в ``commands/_common.build_apply_plan``).
Этап 2 (порог отсечения через существующий ``filter_candidates``) — отдельный
шаг после наблюдения на реальных прогонах, здесь НЕ реализован.

Шкала — 0-100 (переиспользует соглашение ``ScoreOutcome.score_0_100``, issue
явно требует не заводить отдельную шкалу 0-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..search import _tokenize

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile
    from ..search import VacancyCard

# Веса полей профиля при построении набора токенов "что кандидат ищет". skills
# и desired_role — самый прямой сигнал намерения (конкретные технологии/роль),
# summary — общий текст о себе (наименее специфичный, самый шумный источник
# случайных пересечений, см. #490: тема слова ≠ намерение вопроса). highlights
# — достижения, обычно тоже конкретные (проекты/стек), вес между skills и summary.
_FIELD_WEIGHT_SKILLS = 3.0
_FIELD_WEIGHT_DESIRED_ROLE = 3.0
_FIELD_WEIGHT_HIGHLIGHTS = 2.0
_FIELD_WEIGHT_SUMMARY = 1.0

# Токены короче этого порога (предлоги, союзы, обрывки) отбрасываются из знаменателя
# и из матчинга — иначе случайное совпадение однобуквенных/двухбуквенных токенов
# ("в", "на", "по") давало бы ложный вклад в score, не связанный со смыслом
# (тот же класс риска, что #490 - тема слова вместо намерения).
_MIN_TOKEN_LEN = 3


@dataclass(frozen=True)
class ResumeMatchOutcome:
    """Результат скоринга соответствия резюме вакансии (0-100 + breakdown).

    score_0_100 — доля ВЗВЕШЕННОГО веса токенов профиля, найденных в тексте
    вакансии (recall со стороны профиля, не Jaccard: вакансия почти всегда
    длиннее и разнообразнее профиля, поэтому симметричное пересечение давало бы
    равномерно маленькие числа и было бы бесполезно для калибровки порога
    ~90/100, см. #492). breakdown — вклад по полям профиля (для логов/анализа
    распределения на Этапе 1).
    """

    score_0_100: float
    matched_tokens: tuple[str, ...] = ()
    breakdown: dict[str, float] = field(default_factory=dict)


def _weighted_profile_tokens(profile: AIProfile) -> dict[str, float]:
    """Строит {токен: вес} из полей профиля, суммируя веса при повторах."""
    weighted: dict[str, float] = {}

    def _add(text: str, weight: float) -> None:
        for token in _tokenize(text):
            if len(token) < _MIN_TOKEN_LEN:
                continue
            weighted[token] = weighted.get(token, 0.0) + weight

    _add(profile.summary, _FIELD_WEIGHT_SUMMARY)
    for skill in profile.skills:
        _add(skill, _FIELD_WEIGHT_SKILLS)
    for highlight in profile.highlights:
        _add(highlight, _FIELD_WEIGHT_HIGHLIGHTS)
    _add(profile.desired_role, _FIELD_WEIGHT_DESIRED_ROLE)

    return weighted


def score_resume_match(card: VacancyCard, profile: AIProfile | None) -> ResumeMatchOutcome:
    """Скорит соответствие профиля кандидата тексту вакансии (0-100).

    Пустой профиль (``None`` или все поля пусты) и пустой ``vacancy_text``
    (карточка ещё не несёт текста, см. #492 про источник) дают score 0.0 с
    breakdown, объясняющим причину (``reason``) — иначе на Этапе 1 нельзя
    отличить в логе «0 потому что нет пересечения» от «0 потому что нечем
    было сравнивать», что делает калибровку порога Этапа 2 ненадёжной.
    """
    if profile is None:
        return ResumeMatchOutcome(score_0_100=0.0, breakdown={"reason": -1.0})

    weighted = _weighted_profile_tokens(profile)
    if not weighted:
        return ResumeMatchOutcome(score_0_100=0.0, breakdown={"reason": -1.0})

    vacancy_text = card.vacancy_text or ""
    vacancy_tokens = {t for t in _tokenize(vacancy_text) if len(t) >= _MIN_TOKEN_LEN}
    if not vacancy_tokens:
        return ResumeMatchOutcome(score_0_100=0.0, breakdown={"reason": -2.0})

    total_weight = sum(weighted.values())
    matched = [token for token in weighted if token in vacancy_tokens]
    matched_weight = sum(weighted[token] for token in matched)

    score = (matched_weight / total_weight) * 100.0
    return ResumeMatchOutcome(
        score_0_100=score,
        matched_tokens=tuple(sorted(matched)),
        breakdown={
            "matched_weight": matched_weight,
            "total_weight": total_weight,
        },
    )
