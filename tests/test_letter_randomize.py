"""Рандомизация шаблонов писём {a|b|c} (#86).

Синтаксис ``{вариант1|вариант2|вариант3}`` → при каждом рендере один случайный
вариант (``random.choice``). Применяется ДО плейсхолдеров ``{vacancy_title}``/
``{company_name}``, чтобы они не путались с альтернативами (одиночный плейсхолдер
без ``|`` не матчится и остаётся как есть — обратная совместимость).

ТДД-контракты #86:
  - ``{a|b|c}`` → ровно один из вариантов a/b/c.
  - ``{vacancy_title}`` без ``|`` НЕ трогается (регресс плейсхолдеров).
  - ``{Привет|Здравствуйте}`` + ``{vacancy_title}`` вместе — приветствие
    подставлено, title подставлен.
  - Пустая альтернатива (``{a||c}``) → один из вариантов, включая пустую строку.
  - Работает и для AI-fallback (через тот же шаблон), и для AI-промпта
    (если в AI-profile-поле есть ``{a|b|c}``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.apply.letter import (
    TemplateCoverLetterProvider,
    _format_template,
    render_cover_letter,
)
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit

_GREETINGS = ["Привет", "Здравствуйте", "Добрый день"]


def _card(title: str = "Dev", company: str = "Acme") -> VacancyCard:
    return VacancyCard(vacancy_id="1", title=title, company=company, url="https://hh.ru/vacancy/1")


# --- ядро рандомизации ---


@pytest.mark.parametrize("choice_index", [0, 1, 2])
def test_alternatives_pick_one_of_options(choice_index):
    # Зафиксированный выбор → точно предсказуемый вариант из трёх.
    template = "{Привет|Здравствуйте|Добрый день}!"
    picked = _GREETINGS[choice_index]
    with patch("hhru_bot.apply.letter.random.choice", return_value=picked):
        assert _format_template(template, _card()) == f"{picked}!"


def test_alternatives_eventually_yield_different_results():
    # Без фикстуры — за много рендеров должны встретиться все варианты (random,
    # но на 60 попытках по 3 вариантам это надёжно). Это и есть «разный вывод».
    template = "{a|b|c}"
    seen = {_format_template(template, _card()) for _ in range(60)}
    assert seen == {"a", "b", "c"}


def test_placeholder_without_pipe_is_not_randomized():
    # {vacancy_title} без | — регресс плейсхолдера, не альтернатива.
    rendered = _format_template("Вакансия: {vacancy_title}", _card("Python Dev", "Acme"))
    assert rendered == "Вакансия: Python Dev"


def test_alternatives_combined_with_placeholder():
    # {Привет|Здравствуйте}, меня заинтересовала {vacancy_title}
    template = "{Привет|Здравствуйте}, меня заинтересовала {vacancy_title}"
    with patch("hhru_bot.apply.letter.random.choice", return_value="Здравствуйте"):
        rendered = _format_template(template, _card("Python Dev", "Acme"))
    assert rendered == "Здравствуйте, меня заинтересовала Python Dev"


def test_two_alternative_groups_both_resolved():
    template = "{Привет|Здравствуйте}! Пишу по {вакансии|позиции} {vacancy_title}"
    with patch("hhru_bot.apply.letter.random.choice", side_effect=["Привет", "позиции"]):
        rendered = _format_template(template, _card("Dev", "Acme"))
    assert rendered == "Привет! Пишу по позиции Dev"


def test_empty_alternative_is_allowed():
    # {a||c} — средняя пустая альтернатива валидна, может выпасть пустая строка.
    with patch("hhru_bot.apply.letter.random.choice", return_value=""):
        assert _format_template("X{a||c}Y", _card()) == "XY"


def test_braces_inside_alternative_do_not_match():
    # Класс [^{}] запрещает {/} внутри группы альтернатив. Поэтому {a|{x}}
    # (вложенность) внешней группой НЕ матчится — random.choice не вызывается и
    # текст возвращается _resolve_alternatives нетронутым. Этот инвариант
    # критичен: он гарантирует, что выбранная альтернатива никогда не содержит
    # {x}, а значит последующий .format(...) не получит KeyError от случайной
    # скобки внутри варианта (внутри валидной {a|b} группы скобок быть не может).
    from hhru_bot.apply.letter import _resolve_alternatives

    with patch("hhru_bot.apply.letter.random.choice") as mock_choice:
        assert _resolve_alternatives("pre {a|{x}} post") == "pre {a|{x}} post"
    mock_choice.assert_not_called()


def test_single_option_inside_braces_not_matched():
    # {vacancy_title} — единственный «вариант» без |, не матчится как альтернатива,
    # random.choice не вызывается.
    with patch("hhru_bot.apply.letter.random.choice") as mock_choice:
        rendered = _format_template("Привет, {company_name}", _card("Dev", "Acme"))
    assert rendered == "Привет, Acme"
    mock_choice.assert_not_called()


# --- публичный API: render_cover_letter и TemplateCoverLetterProvider ---


def test_render_cover_letter_randomizes_alternatives():
    template = "{Привет|Здравствуйте}, {company_name}!"
    with patch("hhru_bot.apply.letter.random.choice", return_value="Здравствуйте"):
        assert render_cover_letter(template, _card("Dev", "Acme")) == "Здравствуйте, Acme!"


def test_template_provider_randomizes_alternatives():
    provider = TemplateCoverLetterProvider("{Привет|Здравствуйте} {vacancy_title}")
    with patch("hhru_bot.apply.letter.random.choice", return_value="Привет"):
        outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.text == "Привет Dev"
    assert outcome.variant == "template"


# --- AI: рандомизация в fallback-шаблоне и в промпте из AI-profile ---


class _FailingLLM:
    """Мок LLMClient, бросающий при chat (сбой AI → fallback на шаблон)."""

    def chat(self, messages, **params):  # noqa: ARG002
        raise ConnectionError("LLM недоступен")


class _CapturingLLM:
    """Мок LLMClient: запоминает user-сообщение промпта, возвращает непустой контент."""

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, messages, **params):  # noqa: ARG002
        self.prompts.append(messages[1]["content"])
        return NormalizedResponse(content="AI письмо", tool_calls=None, finish_reason="stop")


def test_ai_fallback_template_is_randomized():
    from hhru_bot.ai.letters import AICoverLetterProvider

    provider = AICoverLetterProvider(
        llm_client=_FailingLLM(),
        resume_profile=None,
        fallback_template="{Привет|Здравствуйте}, {company_name}!",
    )
    with patch("hhru_bot.apply.letter.random.choice", return_value="Здравствуйте"):
        outcome = provider.render(_card("Dev", "Acme"), resume_profile=None)
    assert outcome.variant == "ai_fallback"
    assert outcome.text == "Здравствуйте, Acme!"


def test_ai_prompt_randomizes_alternatives_from_profile_summary():
    # Если в AI-profile.summary есть {a|b|c}, рандомизация применяется и к
    # тексту, который уходит в промпт (п.3 ишью: работает и для AI-промпта).
    from hhru_bot.ai.letters import AICoverLetterProvider
    from hhru_bot.config_sections.ai_profile import AIProfile

    llm = _CapturingLLM()
    profile = AIProfile(summary="{Опытный|Сеньорный} бэкенд-разработчик", skills=["python"])
    provider = AICoverLetterProvider(llm_client=llm, resume_profile=profile)

    with patch("hhru_bot.apply.letter.random.choice", return_value="Сеньорный"):
        provider.render(_card("Dev", "Acme"), resume_profile=None)

    assert llm.prompts, "промпт не был собран"
    prompt = llm.prompts[0]
    assert "Сеньорный бэкенд-разработчик" in prompt
    # Ни одной неразрешённой альтернативы в промпте не осталось.
    assert "{" not in prompt and "|" not in prompt
