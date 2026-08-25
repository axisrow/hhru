"""AI-генерация сопроводительного письма под вакансию (#17).

Использует реальный transport-слой LLM из #16 (PR #48, слит в main):
``LLMClient(ai_config).chat(messages, **params) -> NormalizedResponse``.
Контракт: ``NormalizedResponse.content: str | None`` — контент МОЖЕТ прийти
None (timeout / content_filter / length без текста). Поэтому None-контент
обрабатывается как точка fallback на статичный шаблон — это и есть «fallback
на шаблон» из заголовка #17. None НЕ считается успехом.

``hermes-agent-axisrow`` НЕ нужен для импорта этого модуля — LLMClient
импортируется из ``hhru_bot.ai`` (он сам лениво тянет пакет только в момент
построения клиента). Базовая установка hhru-bot без группы [ai] всё ещё
импортирует letters.py; ошибка ImportError всплывёт только при реальной
попытке построить LLMClient без пакета — и это ответственность вызывающей
стороны (commands), не letters.

Главный инвариант: любая ошибка AI (исключение, None-контент, пустой текст)
→ fallback на статичный шаблон БЕЗ исключения. Сбой генерации письма не
должен валить отклик целиком (иначе автоматизация hh.ru теряет заявку из-за
временной недоступности LLM).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..apply.letter import (
    VARIANT_AI,
    VARIANT_AI_FALLBACK,
    CoverLetterProvider,
    LetterOutcome,
    _format_template,
    _log_letter_match,
    _resolve_alternatives,
)
from ..search import VacancyCard

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile
    from . import LLMClient

logger = logging.getLogger("hhru_bot.ai.letters")


class AICoverLetterProvider(CoverLetterProvider):
    """Генерация письма через LLM (#16), с устойчивым fallback на шаблон.

    llm_client — готовый ``LLMClient`` (построен в commands из AppConfig.ai).
    fallback_template — статичный шаблон с плейсхолдерами {vacancy_title}/
    {company_name}, на который откатываемся при любом сбое AI (исключение,
    None-контент, пустой текст). resume_profile — данные кандидата для
    релевантного промпта; None → генерация по вакансии без профиля.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        resume_profile: AIProfile | None = None,
        fallback_template: str = "",
        *,
        temperature: float = 0.7,
    ):
        self._llm = llm_client
        self._profile = resume_profile
        self._fallback_template = fallback_template
        self._temperature = temperature

    def render(
        self,
        vacancy: VacancyCard,
        resume_profile: AIProfile | None = None,
    ) -> LetterOutcome:
        profile = resume_profile or self._profile
        messages = _build_prompt(vacancy, profile)
        try:
            response = self._llm.chat(messages, temperature=self._temperature)
        except Exception as e:  # noqa: BLE001 — намеренно широкий: любой сбой AI → fallback
            logger.warning(
                "AI letter generation failed for '%s': %s — fallback to template",
                vacancy.title,
                e,
            )
            return self._fallback(vacancy)

        # content может быть None (timeout / content_filter / length без текста).
        # None и пустой/только-пробелы текст → fallback, НЕ успех.
        content = response.content if response is not None else None
        letter = (content or "").strip()
        if not letter:
            logger.warning(
                "AI returned empty letter for '%s' (finish_reason=%s) — fallback to template",
                vacancy.title,
                getattr(response, "finish_reason", "?"),
            )
            return self._fallback(vacancy)

        outcome = LetterOutcome(text=letter, variant=VARIANT_AI)
        _log_letter_match(vacancy, outcome.text)
        return outcome

    def _fallback(self, vacancy: VacancyCard) -> LetterOutcome:
        outcome = LetterOutcome(
            text=_format_template(self._fallback_template, vacancy),
            variant=VARIANT_AI_FALLBACK,
        )
        _log_letter_match(vacancy, outcome.text)
        return outcome


def _build_prompt(vacancy: VacancyCard, profile: AIProfile | None) -> list[dict[str, str]]:
    """Собирает chat-completion сообщения: системная инструкция + контекст.

    Контекст вакансии берём из VacancyCard (title/company) — подтверждённые
    данные со страницы поиска. Полный текст вакансии (data-qa
    vacancy-description) требует доп. захода на страницу (троттлинг) и живёт в
    Этапе 4; здесь v1 обходимся карточкой, чтобы не плодить лишних запросов к
    hh.ru (анти-фрод: меньше запросов — ниже риск детекта).

    #86: поля профиля (summary/skills/highlights/desired_role) и few-shot-
    примеры (cover_letter_examples) могут содержать альтернативы {a|b|c} —
    рандомизируем их (_resolve_alternatives), чтобы промпт (и, значит,
    генерируемое письмо) варьировался между запусками.

    #96: cover_letter_examples подаются как few-shot — отдельное user-сообщение
    с образцами стиля ДО запроса письма, чтобы LLM имитировал тон автора. Пусто
    = без few-shot (поведение #17 не меняется, отдельное сообщение не пишется).
    """
    system = (
        "Ты помогаешь писать короткие сопроводительные письма для отклика на "
        "вакансии на hh.ru. Пиши на русском, вежливо, без воды и без выдуманных "
        "фактов о кандидате. Только текст письма, без пояснений и без темы."
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # #96: few-shot стиль — образцы прошлых писём идут первым user-сообщением,
    # до контекста вакансии и запроса. Рандомизация {a|b|c} (#86) применяется к
    # каждому примеру до подстановки — примеры тоже варьируются между запусками.
    if profile is not None and profile.cover_letter_examples:
        examples = [_resolve_alternatives(e) for e in profile.cover_letter_examples]
        body = "\n\n".join(f"Пример {i + 1}:\n{e}" for i, e in enumerate(examples))
        messages.append(
            {
                "role": "user",
                "content": (
                    "Вот несколько моих прошлых сопроводительных писем. "
                    "Старайся писать новое письмо в том же стиле, тоне и структуре:\n\n" + body
                ),
            }
        )

    lines = [f"Вакансия: {vacancy.title}.", f"Компания: {vacancy.company or 'не указана'}."]
    description = getattr(vacancy, "vacancy_description", "") or ""
    if description:
        lines.append("Полное описание вакансии (источник — страница вакансии):\n" + description)
    if profile is not None:
        # Рандомизация {a|b|c} в каждом поле профиля — до подстановки в промпт.
        summary = _resolve_alternatives(profile.summary)
        skills = [_resolve_alternatives(s) for s in profile.skills]
        highlights = [_resolve_alternatives(h) for h in profile.highlights]
        desired_role = _resolve_alternatives(profile.desired_role)
        if summary:
            lines.append(f"О кандидате: {summary}.")
        if skills:
            lines.append("Ключевые навыки: " + ", ".join(skills) + ".")
        if highlights:
            lines.append("Достижения: " + "; ".join(highlights) + ".")
        if desired_role:
            lines.append(f"Желаемая роль: {desired_role}.")
        if profile.tone == "friendly":
            lines.append("Тон: дружелюбный, но профессиональный.")
        else:
            lines.append("Тон: формальный.")
    lines.append("Напиши сопроводительное письмо под эту вакансию.")
    messages.append({"role": "user", "content": "\n".join(lines)})

    return messages
