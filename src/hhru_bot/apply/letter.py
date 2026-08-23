"""Сопроводительное письмо: абстракция провайдера + дефолт-реализация (#17).

Владелец: #17. pipeline и другие шаги этот файл не трогают — кроме минимальной
прокидки опц. letter_provider через ApplyContext (см. pipeline.py).

Абстракция CoverLetterProvider.render(vacancy, resume_profile) -> LetterOutcome
подключается ровно в точке render_cover_letter (pipeline._run). Дефолтная
реализация — текущий .format(...) как офлайн-fallback (полная обратная
совместимость). AI-реализация — в ai/letters.py, использует LLMClient из #16.

apply.py НЕ знает, статичный провайдер или LLM: единственная точка — передача
опц. provider в render_cover_letter. provider=None → старый .format.

LetterOutcome.variant — A/B-признак для истории (actions.letter_variant):
'template' (статичный шаблон) / 'ai' (LLM-генерация) / 'ai_fallback' (LLM не
сработал, откатились на шаблон).

#86: рандомизация альтернатив ``{a|b|c}`` → ``random.choice`` (``_resolve_
alternatives``). Применяется в ``_format_template`` ДО плейсхолдеров — значит,
работает сразу для статичного шаблона, для AI-fallback-шаблона (тоже через
``_format_template``) и для AI-промпта (``ai/letters._build_prompt`` применяет
``_resolve_alternatives`` к полям профиля). Одиночный плейсхолдер ``{vacancy_
title}`` без ``|`` не матчится и остаётся для ``.format``.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.apply.letter")

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile


# Варианты письма для A/B-среза (срез конверсии, Этап 3).
VARIANT_TEMPLATE = "template"  # статичный .format (дефолт/офлайн)
VARIANT_AI = "ai"  # LLM сгенерировал письмо
VARIANT_AI_FALLBACK = "ai_fallback"  # LLM не сработал → откатились на шаблон


@dataclass(frozen=True)
class LetterOutcome:
    """Результат генерации письма: текст + вариант (для A/B-истории)."""

    text: str
    variant: str


@runtime_checkable
class CoverLetterProvider(Protocol):
    """Генератор сопроводительного письма под вакансию.

    render должен быть устойчив: любая ошибка AI-провайдера обязана
    обрабатываться внутри (fallback на шаблон), НЕ пробрасываться наверх —
    сбой генерации письма не должен валить отклик целиком.
    """

    def render(
        self,
        vacancy: VacancyCard,
        resume_profile: AIProfile | None = None,
    ) -> LetterOutcome: ...


class TemplateCoverLetterProvider:
    """Дефолтный провайдер: статичный шаблон с плейсхолдерами .format.

    Офлайн-fallback. Полная обратная совместимость со старым render_cover_letter —
    тот же .format(vacancy_title=..., company_name=...). resume_profile
    игнорируется (шаблон не персонализируется).
    """

    def __init__(self, template: str):
        self._template = template

    def render(
        self,
        vacancy: VacancyCard,
        resume_profile: AIProfile | None = None,  # noqa: ARG002
    ) -> LetterOutcome:
        outcome = LetterOutcome(
            text=_format_template(self._template, vacancy),
            variant=VARIANT_TEMPLATE,
        )
        _log_letter_match(vacancy, outcome.text)
        return outcome


# Рандомизация альтернатив {a|b|c} (#86): матчит {вариант1|вариант2|...} —
# обязателен хотя бы один '|', альтернативы могут быть пустыми ({a||c} → иногда
# пусто). Внутри группы не допускаются фигурные скобки (вложенность не нужна).
# Одиночный плейсхолдер {vacancy_title} (без '|') не матчится — остаётся для .format.
_ALTERNATIVES_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def _resolve_alternatives(text: str) -> str:
    """Заменяет каждую группу ``{a|b|c}`` на один случайный вариант (random.choice).

    Применяется ДО плейсхолдеров ``{vacancy_title}``/``{company_name}``: так
    одиночный плейсхолдер (без ``|``) не матчится этим регекспом и доходит до
    ``.format`` нетронутым. Снижает шаблонность писём — анти-фрод + AI-детекторы
    (HIGH-идея №2 из #84, паттерн s3rgeym ``rand_text()``). random без seed:
    каждый рендер — разный вывод.
    """
    return _ALTERNATIVES_RE.sub(lambda m: random.choice(m.group(1).split("|")), text)


def _format_template(template: str, vacancy: VacancyCard) -> str:
    return _resolve_alternatives(template).format(
        vacancy_title=vacancy.title, company_name=vacancy.company
    )


def render_cover_letter(
    template: str,
    vacancy: VacancyCard,
    provider: CoverLetterProvider | None = None,
) -> str:
    """Рендерит письмо. Единственная точка подключения провайдера (#17).

    provider=None (по умолчанию) → статичный .format (характеризация, обратная
    совместимость — поведение не меняется). provider задан → делегирует ему;
    AI-провайдер сам отвечает за fallback, поэтому исключения не ждём.

    Возвращает только текст письма. variant нужен истории — вызывающая сторона
    (pipeline) берёт его отдельно через provider.render(...).variant, когда
    провайдер задан; без провайдера вариант всегда 'template'.
    """
    if provider is None:
        letter = _format_template(template, vacancy)
        _log_letter_match(vacancy, letter)
        return letter
    return provider.render(vacancy).text


def _log_letter_match(vacancy: VacancyCard, letter: str) -> None:
    """Log observation-only letter↔vacancy keyword score (#493, stage 1)."""
    from ..scoring import letter_match_score

    try:
        outcome = letter_match_score(vacancy, letter)
    except Exception as exc:  # noqa: BLE001 — observation must not break apply
        logger.warning(
            "letter-match failed for %s '%s': %s", vacancy.vacancy_id, vacancy.title, exc
        )
        return
    logger.info(
        "letter-match %s '%s': %.1f/100 (%s)",
        vacancy.vacancy_id,
        vacancy.title,
        outcome.score_0_100,
        outcome.rationale,
    )
