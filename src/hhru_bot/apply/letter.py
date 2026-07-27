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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..search import VacancyCard

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
        return LetterOutcome(
            text=_format_template(self._template, vacancy),
            variant=VARIANT_TEMPLATE,
        )


def _format_template(template: str, vacancy: VacancyCard) -> str:
    return template.format(vacancy_title=vacancy.title, company_name=vacancy.company)


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
        return _format_template(template, vacancy)
    return provider.render(vacancy).text
