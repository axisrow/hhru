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

**Наследуемое ограничение (ревью PR #549, Codex adversarial-review):**
переиспользуемая ``_is_negated`` распознаёт только смежную пару
маркер+требование («Python не требуется»), поэтому отрицание со вставленным
словом («Python БОЛЬШЕ не требуется», «Python УЖЕ не требуется») не
распознаётся и такое письмо всё ещё скорится как полное совпадение. Это НЕ
новый дефект letter-match — тот же случай уже зафиксирован как открытый,
неисправленный в ``resume_match._is_negated`` (см. её докстринг, #490): закрыть
его значит перейти на sentence-aware разбор границ вместо плоского окна
токенов, а это архитектурное изменение вне Тир-0/переиспользования, которых
требует issue #493. Оставлено как унаследованный follow-up, а не молча
проигнорировано.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .resume_match import NO_DATA_RATIONALE, _is_negated, _matched_ratio, _tokenize
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
    """
    letter_tokens = _tokenize(letter_text)
    kept = [t for i, t in enumerate(letter_tokens) if not _is_negated(letter_tokens, i)]
    return " ".join(kept)


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

    score = round(_matched_ratio(affirmed_letter_text, vacancy_tokens) * 100.0, 2)
    rationale = "keyword-match письма" if score > 0.0 else "совпадений нет"
    return ScoreOutcome(
        score_0_100=score,
        mode=LETTER_MATCH_MODE,
        rationale=rationale,
        breakdown={"letter": score},
    )
