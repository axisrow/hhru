"""Few-shot стиль писём через cover_letter_examples (#96, расширение #17).

``AIProfile.cover_letter_examples`` (issue #96) — список прошлых писем как
образцы стиля. В ``_build_prompt`` они подаются как few-shot: LLM имитирует
тон/структуру автора. Опционально (пусто = без few-shot, поведение #17).

ТДД-контракты #96:
  - examples есть → промпт содержит каждый пример и явный призыв писать в их стиле.
  - examples пуст → промпт не упоминает few-shot/примеры (регресс #17).
  - examples могут содержать альтернативы ``{a|b|c}`` (#86) → они рандомизируются.
  - fallback на шаблон (#17) сохранён: AI-сбой с examples → статичный шаблон.
"""

from __future__ import annotations

from unittest.mock import patch

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.search import VacancyCard


def _card(title: str = "Dev", company: str = "Acme") -> VacancyCard:
    return VacancyCard(vacancy_id="1", title=title, company=company, url="https://hh.ru/vacancy/1")


class _CapturingLLM:
    """Мок LLMClient: запоминает собранный промпт, возвращает непустой контент."""

    def __init__(self):
        self.prompts: list[list[dict]] = []

    def chat(self, messages, **params):  # noqa: ARG002
        self.prompts.append(messages)
        return NormalizedResponse(
            content="AI письмо в стиле примеров", tool_calls=None, finish_reason="stop"
        )


class _FailingLLM:
    """Мок LLMClient, бросающий при chat (сбой AI → fallback на шаблон)."""

    def chat(self, messages, **params):  # noqa: ARG002
        raise ConnectionError("LLM недоступен")


# --- examples есть → промпт содержит их как few-shot ---


def test_examples_appear_in_prompt_as_style_samples():
    from hhru_bot.ai.letters import AICoverLetterProvider

    examples = [
        "Здравствуйте! Очень откликает ваша вакансия, пишу как бэкенд-разработчик.",
        "Добрый день. Мой опыт в python и django релевантен вашей задаче.",
    ]
    llm = _CapturingLLM()
    profile = AIProfile(summary="Бэкенд-разработчик", cover_letter_examples=examples)
    provider = AICoverLetterProvider(llm_client=llm, resume_profile=profile)

    provider.render(_card("Python Dev", "Acme"), resume_profile=None)

    assert llm.prompts, "промпт не был собран"
    prompt = str(llm.prompts[0])
    # Каждый пример дословно попадает в промпт.
    for example in examples:
        assert example in prompt
    # Явный призыв писать в стиле примеров — few-shot-сигнал для LLM.
    assert "стил" in prompt or "стилю" in prompt or "пример" in prompt


def test_examples_become_dedicated_user_message_before_request():
    # Структура few-shot: отдельное сообщение с образцами идёт до финального
    # запроса «напиши письмо», чтобы LLM видел стиль перед генерацией.
    from hhru_bot.ai.letters import AICoverLetterProvider

    llm = _CapturingLLM()
    profile = AIProfile(cover_letter_examples=["Мой прошлый отклик в таком-то тоне."])
    provider = AICoverLetterProvider(llm_client=llm, resume_profile=profile)

    provider.render(_card("Dev", "Acme"), resume_profile=None)

    messages = llm.prompts[0]
    # system + хотя бы ещё два user-сообщения (few-shot-примеры + запрос письма).
    roles = [m["role"] for m in messages]
    assert messages[0]["role"] == "system"
    assert roles.count("user") >= 2


# --- examples пуст → поведение #17 не меняется (регресс) ---


def test_empty_examples_omit_few_shot_from_prompt():
    # Без examples промпт не должен содержать секции про образцы стиля —
    # это и есть «обратная совместимость»: #96 не ломает #17.
    from hhru_bot.ai.letters import AICoverLetterProvider

    llm_no_examples = _CapturingLLM()
    profile = AIProfile(summary="Бэкенд-разработчик")  # cover_letter_examples не задан → []
    assert profile.cover_letter_examples == []
    provider = AICoverLetterProvider(llm_client=llm_no_examples, resume_profile=profile)

    provider.render(_card("Dev", "Acme"), resume_profile=None)

    prompt = str(llm_no_examples.prompts[0])
    assert "пример" not in prompt.lower() or "примеров" not in prompt
    assert "стил" not in prompt.lower()


def test_empty_examples_prompt_matches_pure_profile_prompt():
    # Пустой список examples → промпт побайтово совпадает с тем, что было бы в
    # чистом #17 (тот же профиль, никаких few-shot-сообщений).
    from hhru_bot.ai.letters import _build_prompt

    profile = AIProfile(summary="Бэкенд-разработчик", skills=["python"])
    prompt = _build_prompt(_card("Dev", "Acme"), profile)
    # Системное + ровно одно user-сообщение (запрос), без few-shot-блока.
    assert [m["role"] for m in prompt] == ["system", "user"]
    assert "Бэкенд-разработчик" in prompt[1]["content"]
    assert "пример" not in prompt[1]["content"].lower()


# --- примеры поддерживают рандомизацию {a|b|c} (#86) ---


def test_examples_alternatives_are_randomized_in_prompt():
    # examples могут содержать {a|b|c} — рандомизируются до попадания в промпт.
    from hhru_bot.ai.letters import AICoverLetterProvider

    llm = _CapturingLLM()
    profile = AIProfile(cover_letter_examples=["{Здравствуйте|Добрый день}! Пишу вам по вакансии."])
    provider = AICoverLetterProvider(llm_client=llm, resume_profile=profile)

    with patch("hhru_bot.apply.letter.random.choice", return_value="Добрый день"):
        provider.render(_card("Dev", "Acme"), resume_profile=None)

    # Проверяем содержимое сообщений (content), а не str(list) — в нём всегда
    # есть '{' от синтаксиса dict-сообщений.
    content = "\n".join(m["content"] for m in llm.prompts[0])
    assert "Добрый день! Пишу вам по вакансии." in content
    # Ни одной неразрешённой альтернативы в промпте не осталось.
    assert "{" not in content and "|" not in content


# --- fallback на шаблон сохранён (#17) ---


def test_examples_present_fallback_on_llm_failure():
    # examples есть, но LLM упал → статичный шаблон, variant 'ai_fallback'.
    # Few-shot не меняет контракт устойчивости из #17.
    from hhru_bot.ai.letters import AICoverLetterProvider

    provider = AICoverLetterProvider(
        llm_client=_FailingLLM(),
        resume_profile=AIProfile(cover_letter_examples=["Образец письма."]),
        fallback_template="Здравствуйте, {company_name}!",
    )
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Здравствуйте, Acme!"


def test_examples_present_fallback_on_empty_content():
    # examples есть, но LLM вернул пустой контент → fallback на шаблон.
    from hhru_bot.ai.letters import AICoverLetterProvider

    class _EmptyLLM:
        def chat(self, messages, **params):  # noqa: ARG002
            return NormalizedResponse(content="   ", tool_calls=None, finish_reason="length")

    provider = AICoverLetterProvider(
        llm_client=_EmptyLLM(),
        resume_profile=AIProfile(cover_letter_examples=["Образец письма."]),
        fallback_template="Шаблон: {vacancy_title}",
    )
    outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Шаблон: Dev"
