"""Сопроводительное письмо: дефолт-провайдер (characterization) + #17.

#17 вводит абстракцию CoverLetterProvider: render(vacancy, resume_profile)
возвращает LetterOutcome(text, variant). Дефолтная реализация — текущий
.format (офлайн-fallback, полная обратная совместимость). AI-реализация —
в ai/letters.py, тестится тут через мок LLMClient (реальный транспорт из #16).

ТДД-контракты #17:
  - AI-успех (content непустой) → письмо под вакансию, variant='ai'.
  - AI None-контент (timeout/content_filter) → fallback БЕЗ исключения, 'ai_fallback'.
  - AI-ошибка (исключение из chat) → fallback БЕЗ исключения, 'ai_fallback'.
  - AI пустой ответ → fallback, 'ai_fallback'.
  - дефолт-провайдер (.format) → variant='template', поведение как прежде.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.apply import render_cover_letter
from hhru_bot.apply.letter import (
    CoverLetterProvider,
    LetterOutcome,
    TemplateCoverLetterProvider,
)
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def _card(title: str, company: str) -> VacancyCard:
    return VacancyCard(vacancy_id="1", title=title, company=company, url="https://hh.ru/vacancy/1")


# --- characterization: render_cover_letter не изменился (#17 не ломает API) ---


def test_render_cover_letter_substitutes_placeholders():
    template = "Вакансия: {vacancy_title}, компания: {company_name}"
    rendered = render_cover_letter(template, _card("Python Dev", "Acme"))
    assert rendered == "Вакансия: Python Dev, компания: Acme"


def test_render_cover_letter_no_placeholders():
    assert render_cover_letter("Привет", _card("X", "Y")) == "Привет"


def test_render_cover_letter_multiline_example():
    template = "Здравствуйте!\nВакансия: {vacancy_title}"
    assert render_cover_letter(template, _card("DevOps", "Z")) == (
        "Здравствуйте!\nВакансия: DevOps"
    )


# --- дефолт-провайдер: текущий .format как офлайн-fallback (#17) ---


def test_template_provider_substitutes_placeholders():
    provider = TemplateCoverLetterProvider("Письмо для {vacancy_title} / {company_name}")
    outcome = provider.render(_card("Python Dev", "Acme"), resume_profile=None)
    assert outcome.text == "Письмо для Python Dev / Acme"
    assert outcome.variant == "template"


def test_template_provider_works_without_resume_profile():
    # resume_profile опционален для дефолтного провайдера — он не использует его.
    provider = TemplateCoverLetterProvider("Привет, {company_name}")
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "template"


# --- AI-провайдер через мок LLMClient (#16 контракт: chat()->NormalizedResponse) ---


class _RecordingLLM:
    """Мок LLMClient.chat(messages, **params) -> NormalizedResponse.

    Имитирует реальный транспорт #16: возвращает NormalizedResponse с заданным
    content (может быть None), записывает вызовы для проверок промпта.
    """

    def __init__(self, content: str | None, finish_reason: str = "stop"):
        self._content = content
        self._finish_reason = finish_reason
        self.calls: list[tuple[list[dict], dict]] = []

    def chat(self, messages, **params):
        self.calls.append((messages, params))
        return NormalizedResponse(
            content=self._content, tool_calls=None, finish_reason=self._finish_reason
        )


class _FailingLLM:
    """Мок LLMClient, бросающий при chat (сетевая ошибка/таймаут/timeout SDK)."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def chat(self, messages, **params):  # noqa: ARG002
        raise self.exc


def test_ai_provider_success_personalized_letter():
    from hhru_bot.ai.letters import AICoverLetterProvider

    llm = _RecordingLLM("Здравствуйте! Мой опыт в Python идеально подходит для Python Dev.")
    provider = AICoverLetterProvider(llm_client=llm, resume_profile=None)
    outcome = provider.render(_card("Python Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai"
    assert "Python" in outcome.text
    # LLM реально вызывался, промпт содержал контекст вакансии.
    assert llm.calls, "LLM не был вызван"
    prompt = str(llm.calls[0][0])
    assert "Python Dev" in prompt or "Acme" in prompt


def test_ai_provider_none_content_falls_back_to_template():
    # content=None (timeout/content_filter/length без текста) → fallback, НЕ успех.
    from hhru_bot.ai.letters import AICoverLetterProvider

    provider = AICoverLetterProvider(
        llm_client=_RecordingLLM(None, finish_reason="content_filter"),
        resume_profile=None,
        fallback_template="Здравствуйте, {company_name}!",
    )
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Здравствуйте, Acme!"


def test_ai_provider_exception_falls_back_without_raising():
    from hhru_bot.ai.letters import AICoverLetterProvider

    provider = AICoverLetterProvider(
        llm_client=_FailingLLM(ConnectionError("LLM недоступен")),
        resume_profile=None,
        fallback_template="Здравствуйте, {company_name}!",
    )
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Здравствуйте, Acme!"


def test_ai_provider_empty_response_falls_back_to_template():
    from hhru_bot.ai.letters import AICoverLetterProvider

    provider = AICoverLetterProvider(
        llm_client=_RecordingLLM("   "),  # пустой/только-пробелы content
        resume_profile=None,
        fallback_template="Шаблон: {vacancy_title}",
    )
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Шаблон: Dev"


def _normalized_llm_outcome(chat_response, title="Dev"):
    """Пропустить ответ через реальный нормализатор и отрендерить письмо.

    end-to-end #230: форма ответа Hermes → _normalize_response →
    AICoverLetterProvider. Если нормализатор оставит отказ/фильтрацию в content,
    провайдер увидит непустой текст и отправит его работодателю — тест это ловит.
    """
    from hhru_bot.ai.letters import AICoverLetterProvider
    from hhru_bot.ai.llm_client import _normalize_response

    normalized = _normalize_response(chat_response)

    class _NormalizedLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            return normalized

    provider = AICoverLetterProvider(
        llm_client=_NormalizedLLM(),
        resume_profile=None,
        fallback_template="Шаблон: {vacancy_title}",
    )
    return provider.render(_card(title, "Acme"), resume_profile=None)


def test_ai_provider_refusal_falls_back_not_sent_to_employer():
    # Регрессия #230: structured-refusal (пустой content + refusal) не должен уйти
    # работодателю как письмо. Нормализатор держит отказ ВНЕ content → fallback.
    chat = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, refusal="не могу написать письмо"),
                finish_reason="stop",
            )
        ]
    )
    outcome = _normalized_llm_outcome(chat)
    assert outcome.variant == "ai_fallback"
    assert "не могу" not in outcome.text


def test_ai_provider_content_filter_with_text_falls_back_not_sent():
    # content_filter с непустым текстом → нормализатор очищает content → fallback,
    # а не отправка частичного отфильтрованного текста работодателю (#230).
    chat = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="частичный отфильтрованный текст", refusal=None),
                finish_reason="content_filter",
            )
        ]
    )
    outcome = _normalized_llm_outcome(chat)
    assert outcome.variant == "ai_fallback"
    assert "отфильтрованный" not in outcome.text


def test_ai_provider_mixed_content_and_refusal_falls_back_not_sent():
    # content + refusal вместе — тоже отказ: нормализатор очищает content → fallback.
    chat = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="некий текст", refusal="не могу помочь"),
                finish_reason="stop",
            )
        ]
    )
    outcome = _normalized_llm_outcome(chat)
    assert outcome.variant == "ai_fallback"
    assert "не могу" not in outcome.text


# --- render_cover_letter делегирует провайдеру, если он передан (#17) ---


def test_render_cover_letter_uses_provider_when_given():
    # Единственная точка подключения (pipeline._run): если провайдер передан,
    # render_cover_letter делегирует ему; без провайдера — старый .format.
    class _SpyProvider(CoverLetterProvider):
        def __init__(self):
            self.called = False

        def render(self, vacancy, resume_profile=None):  # noqa: ARG002
            self.called = True
            return LetterOutcome(text="from-spy", variant="ai")

    spy = _SpyProvider()
    result = render_cover_letter("ignored template", _card("X", "Y"), provider=spy)
    assert result == "from-spy"
    assert spy.called


def test_render_cover_letter_template_when_no_provider():
    # provider=None (по умолчанию) → старое поведение .format.
    result = render_cover_letter("Hi {company_name}", _card("X", "Acme"))
    assert result == "Hi Acme"
