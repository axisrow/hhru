"""Keyword-скоринг соответствия сопроводительного письма вакансии (#493).

Эта функция намеренно переиспользует токенизацию, стем-матч и обработку
отрицаний из ``resume_match``. Письмо — единственный источник токенов
кандидата; в отличие от resume-match, здесь не нужен ``AIProfile`` и нет
взвешенных факторов.

Этап 1 — только вычисление и логирование score, без порога и без блокировки
отправки. Как и resume-match, результат использует общую шкалу
``ScoreOutcome.score_0_100``.

**Отрицание в самом письме тоже снимает совпадение (найдено ревью PR #549,
Codex).** ``_matched_ratio(profile_text, vacancy_tokens)`` спроектирована для
``AIProfile``: там отрицание в тексте профиля — шум («без опыта» в summary не
навык), и функция намеренно отбрасывает такие токены из profile_text, проверяя
отрицание только в vacancy_tokens. Для письма кандидата это неверно: письмо —
не «облако токенов», а связный текст, где кандидат сам может явно ОТКАЗАТЬСЯ
от навыка («без Python», «Python не требуется мне»). Пропущенное через
``_matched_ratio`` как есть, такое письмо давало 100.0 против вакансии
«Требуется Python» — отрицание молча терялось, потому что _matched_ratio
фильтрует маркеры отрицания именно из первого (letter/profile) аргумента.
Фикс: токены письма, стоящие под отрицанием В САМОМ ПИСЬМЕ (по той же
``_is_negated``, что resume_match применяет к вакансии), исключаются ДО
передачи в ``_matched_ratio`` — так «без Python» не подтверждает навык
«python» у кандидата, а «Python» без отрицания — подтверждает, как раньше.

**Обновление (после мерджа #550 в main):** отрицание со вставленным словом
(«Python БОЛЬШЕ не требуется») ранее не распознавалось — это было наследуемое
ограничение ``resume_match._is_negated`` (найдено ревью PR #549, Codex
adversarial-review). PR #550 сделал ``_is_negated``/``_tokenize`` clause-aware
и закрыл именно этот случай; letter_match получает исправление автоматически
через переиспользование, без изменений в этом модуле.

**Clause boundary нужна на ОБЕИХ сторонах, не только у письма (найдено ревью
PR #549, /review, cycle 4).** ``letter_match_score`` изначально токенизировал
``vacancy_text`` простым ``_tokenize()`` и звал ``_matched_ratio`` без
``clause_ids`` — в отличие от ``resume_match_score``, который передаёт их из
``_tokenize_with_boundaries(vacancy_text)``. Из-за этого отрицание из ВТОРОГО
предложения вакансии («Python. Не требуется SQL») ложно гасило совпавший
токен из ПЕРВОГО предложения («python»), хотя в вакансии он ничем не
отрицается. Фикс: ``vacancy_tokens``/``vacancy_clause_ids`` берутся вместе
через ``_tokenize_with_boundaries``, как в ``resume_match_score`` — letter_match
теперь clause-aware симметрично на обеих сторонах сравнения.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .resume_match import (
    NO_DATA_RATIONALE,
    _is_negated,
    _matched_ratio,
    _tokenize,
    _tokenize_with_boundaries,
)
from .types import ScoreOutcome

if TYPE_CHECKING:
    from ..search import VacancyCard


LETTER_MATCH_MODE = "letter_match"


def _drop_letter_negated_tokens(letter_text: str) -> str:
    """Убирает из письма токены, которые кандидат сам отрицает у себя.

    «без Python» / «Python не требуется» в письме — кандидат явно заявляет об
    ОТСУТСТВИИ навыка, а не о его наличии. Такие токены не должны попадать в
    ``_matched_ratio`` как «подтверждённые письмом» — иначе явный отказ от
    навыка засчитывается как идеальное совпадение с требованием вакансии.

    ``clause_ids`` передаются в ``_is_negated`` (фича #509): без них маркер
    отрицания из одного предложения письма гасил бы токен навыка из другого
    («Я знаю Python. Не требуется SQL» ложно вычёркивало python) — найдено
    ревью PR #549 (/review, cycle 3).
    """
    letter_tokens, clause_ids = _tokenize_with_boundaries(letter_text)
    kept = [t for i, t in enumerate(letter_tokens) if not _is_negated(letter_tokens, i, clause_ids)]
    return " ".join(kept)


def letter_match_score(card: VacancyCard, letter: str | None) -> ScoreOutcome:
    """Оценивает долю токенов письма, подтверждённых текстом вакансии.

    Пустое письмо или пустой ``vacancy_text`` дают ``0.0`` с отдельной
    пометкой «нет данных», а не считаются полным несовпадением. Это позволяет
    не смешивать в наблюдаемом распределении реальные нулевые матчи с
    карточками, для которых сопоставление было невозможно.
    """
    vacancy_tokens, vacancy_clause_ids = _tokenize_with_boundaries(
        getattr(card, "vacancy_text", "") or ""
    )
    letter_text = letter or ""
    if not vacancy_tokens or not _tokenize(letter_text):
        return ScoreOutcome(
            score_0_100=0.0,
            mode=LETTER_MATCH_MODE,
            rationale=NO_DATA_RATIONALE,
            breakdown={},
        )

    affirmed_letter_text = _drop_letter_negated_tokens(letter_text)
    if not affirmed_letter_text:
        # Всё, что было в письме, кандидат сам же и отрицал — считать
        # подтверждённым нечего, это честный ноль, а не «нет данных»
        # (letter_text непустой, сопоставление реально происходило).
        return ScoreOutcome(
            score_0_100=0.0,
            mode=LETTER_MATCH_MODE,
            rationale="совпадений нет",
            breakdown={"letter": 0.0},
        )

    score = round(
        _matched_ratio(affirmed_letter_text, vacancy_tokens, vacancy_clause_ids) * 100.0, 2
    )
    rationale = "keyword-match письма" if score > 0.0 else "совпадений нет"
    return ScoreOutcome(
        score_0_100=score,
        mode=LETTER_MATCH_MODE,
        rationale=rationale,
        breakdown={"letter": score},
    )
