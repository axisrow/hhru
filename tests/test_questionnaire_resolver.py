"""Чистая логика сопоставления вопрос -> шаблон -> ответ (#482).

Без браузера, без SQLite, без LLM-пакета: keyword resolver обязан работать при
отсутствующей optional-зависимости .[ai] (критерий приёмки issue).
"""

from __future__ import annotations

import json

import pytest

from hhru_bot.ai.questions import Question
from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.external_forms.detect import normalize
from hhru_bot.questionnaires.resolver import (
    KEYWORD,
    PHRASE,
    ResolvedAnswer,
    TemplateMatch,
    build_answer,
    check_choice_compatibility,
    choice_indices,
    compliance_gate,
    match_keyword,
    match_phrase,
    resolve_template,
)
from hhru_bot.questionnaires.templates import QuestionTemplate

pytestmark = pytest.mark.unit


class _LLM:
    """Минимальный двойник LLMClient: считает вызовы, отдаёт готовый payload."""

    def __init__(self, payload, *, raw: str | None = None):
        self.payload = payload
        self.raw = raw
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        content = self.raw if self.raw is not None else json.dumps(self.payload)
        return NormalizedResponse(content=content, tool_calls=None, finish_reason="stop")


class _BoomLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **_kwargs):
        self.calls += 1
        raise RuntimeError("транспорт недоступен")


def _choice(text: str, *options: str, is_radio: bool = True) -> Question:
    return Question(0, text, "choice", tuple(options), is_radio=is_radio)


def _text(text: str) -> Question:
    return Question(0, text, "text")


def _static(name: str, answer: str, cluster: str = "conditions") -> QuestionTemplate:
    return QuestionTemplate(name, cluster, "static", answer=answer)


def _contextual(name: str, instruction: str, *, cluster: str = "conditions", examples=()):
    return QuestionTemplate(name, cluster, "contextual", instruction=instruction, examples=examples)


# --- match_phrase -----------------------------------------------------------


def test_match_phrase_finds_confirmed_wording():
    match = match_phrase("Ваши зарплатные ожидания?", {"ваши зарплатные ожидания?": "salary"})

    assert match is not None
    assert (match.template, match.source, match.confidence) == ("salary", PHRASE, 1.0)


def test_match_phrase_ignores_case_and_spacing():
    confirmed = {"ваши зарплатные ожидания?": "salary"}

    assert match_phrase("  ВАШИ   Зарплатные\nОжидания?  ", confirmed) is not None


def test_match_phrase_returns_none_for_unknown_text():
    assert match_phrase("Расскажите о себе", {"ваш город": "location"}) is None


def test_match_phrase_carries_seed_cluster():
    match = match_phrase("вопрос", {"вопрос": "salary"})

    assert match is not None and match.cluster == "conditions"


def test_match_phrase_gives_unknown_template_the_default_cluster():
    match = match_phrase("вопрос", {"вопрос": "my_own_template"})

    assert match is not None and match.cluster == "mixed"


# --- match_keyword ----------------------------------------------------------


def test_match_keyword_resolves_salary_question():
    match = match_keyword("Какие у вас зарплатные ожидания?")

    assert match is not None
    assert (match.template, match.source) == ("salary", KEYWORD)
    assert match.confidence == pytest.approx(0.95)


def test_match_keyword_resolves_each_seed_field():
    resolved = {
        match_keyword(text).template
        for text in (
            "Ваши зарплатные ожидания?",
            "В каком городе вы живёте?",
            "Какая желаемая должность?",
            "С какими нишами вы работали?",
        )
    }

    assert resolved == {"salary", "location", "desired_role", "business_segments"}


def test_match_keyword_returns_none_without_any_hit():
    assert match_keyword("Опишите свой самый сложный проект") is None


def test_match_keyword_returns_none_when_two_templates_match():
    """Fail-closed: ответить одним значением на двусмысленный вопрос нельзя."""
    ambiguous = "В каком городе вы работаете и какие зарплатные ожидания?"

    assert match_keyword(ambiguous) is None


# --- resolve_template -------------------------------------------------------


def test_resolve_template_prefers_confirmed_phrase_over_keyword():
    text = "Какие у вас зарплатные ожидания?"

    match = resolve_template(text, confirmed={"какие у вас зарплатные ожидания?": "custom"})

    assert match is not None and match.template == "custom" and match.source == PHRASE


def test_resolve_template_falls_back_to_keyword():
    match = resolve_template("Какие у вас зарплатные ожидания?", confirmed={})

    assert match is not None and match.template == "salary" and match.source == KEYWORD


def test_resolve_template_returns_none_when_nothing_matches():
    assert resolve_template("Что вас мотивирует?", confirmed={}) is None


# --- compliance_gate --------------------------------------------------------


def _compliance_match() -> TemplateMatch:
    return TemplateMatch("work_permit", "compliance", KEYWORD, 0.95)


def test_compliance_gate_allows_non_strict_cluster_without_template():
    assert compliance_gate(None, TemplateMatch("salary", "conditions", KEYWORD, 0.95)) == ""


def test_compliance_gate_blocks_missing_template():
    assert "без сохранённого значения" in compliance_gate(None, _compliance_match())


def test_compliance_gate_blocks_contextual_template():
    template = _contextual("work_permit", "ответь по документам", cluster="compliance")

    assert "только явный static" in compliance_gate(template, _compliance_match())


def test_compliance_gate_blocks_blank_static_answer():
    template = QuestionTemplate("work_permit", "compliance", "static", answer="   ")

    assert "только явный static" in compliance_gate(template, _compliance_match())


def test_compliance_gate_allows_explicit_static_answer():
    template = _static("work_permit", "Гражданство РФ", cluster="compliance")

    assert compliance_gate(template, _compliance_match()) == ""


# --- choice_indices / check_choice_compatibility ----------------------------


def test_choice_indices_match_by_text_not_position():
    question = _choice("Формат работы?", "Офис", "Удалённо", "Гибрид")

    assert choice_indices(question, "Удалённо") == (1,)


def test_choice_indices_survive_reordered_options():
    value = "Удалённо"
    first = _choice("Формат?", "Офис", "Удалённо")
    reordered = _choice("Формат?", "Удалённо", "Офис")

    picked_first = first.options[choice_indices(first, value)[0]]
    picked_second = reordered.options[choice_indices(reordered, value)[0]]

    assert picked_first == picked_second == value


def test_choice_indices_support_multiple_values_for_checkbox():
    question = _choice("Сегменты?", "B2B", "B2C", "B2G", is_radio=False)

    assert choice_indices(question, "B2B, B2G") == (0, 2)


def test_choice_indices_ignore_case_and_spacing():
    question = _choice("Формат?", "Удалённо")

    assert choice_indices(question, "  удалённо ") == (0,)


def test_choice_indices_empty_when_nothing_matches():
    question = _choice("Формат?", "Офис", "Удалённо")

    assert choice_indices(question, "Вахтой") == ()


def test_check_choice_compatibility_accepts_single_radio_pick():
    assert check_choice_compatibility(_choice("q", "a", "b"), (0,)) == ""


def test_check_choice_compatibility_rejects_multiple_radio_picks():
    reason = check_choice_compatibility(_choice("q", "a", "b"), (0, 1))

    assert "один вариант" in reason


def test_check_choice_compatibility_accepts_multiple_checkbox_picks():
    question = _choice("q", "a", "b", is_radio=False)

    assert check_choice_compatibility(question, (0, 1)) == ""


def test_check_choice_compatibility_rejects_empty_selection():
    assert check_choice_compatibility(_choice("q", "a"), ()) != ""


# --- build_answer: static ---------------------------------------------------


def _salary_match() -> TemplateMatch:
    return TemplateMatch("salary", "conditions", KEYWORD, 0.95)


def test_build_answer_static_text_question():
    resolved = build_answer(_text("Ожидания?"), _static("salary", "от 250000"), _salary_match())

    assert resolved.resolved
    assert resolved.answer == "от 250000"
    assert resolved.confidence == 1.0
    assert resolved.answer_source == "profile"
    assert resolved.resolver_source == "static"


def test_build_answer_static_never_calls_llm():
    llm = _LLM({"answer": "generated", "confidence": 1.0})

    build_answer(_text("Ожидания?"), _static("salary", "от 250000"), _salary_match(), llm=llm)

    assert llm.calls == []


def test_build_answer_static_choice_picks_matching_option():
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _static("format", "Удалённо"), _salary_match())

    assert resolved.resolved and resolved.option_indices == (1,)


def test_build_answer_static_choice_pends_when_option_missing():
    question = _choice("Формат?", "Офис", "Гибрид")

    resolved = build_answer(question, _static("format", "Удалённо"), _salary_match())

    assert not resolved.resolved
    assert "ни один вариант" in resolved.pending_reason


def test_build_answer_pends_without_saved_template():
    resolved = build_answer(_text("Ожидания?"), None, _salary_match())

    assert not resolved.resolved
    assert "нет сохранённого ответа" in resolved.pending_reason


def test_build_answer_pends_on_invalid_template():
    broken = QuestionTemplate("salary", "conditions", "static", answer="")

    resolved = build_answer(_text("Ожидания?"), broken, _salary_match())

    assert not resolved.resolved and "непригоден" in resolved.pending_reason


# --- build_answer: contextual ----------------------------------------------


def test_build_answer_contextual_without_llm_pends():
    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match()
    )

    assert not resolved.resolved and "LLM не настроен" in resolved.pending_reason


def test_build_answer_contextual_uses_llm_and_marks_source():
    llm = _LLM({"answer": "от 250000 рублей", "confidence": 0.95})

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert resolved.resolved
    assert resolved.answer == "от 250000 рублей"
    assert resolved.answer_source == "llm"
    assert resolved.resolver_source == "contextual"


def test_contextual_prompt_carries_instruction_and_examples():
    llm = _LLM({"answer": "ответ", "confidence": 0.99})
    template = _contextual("salary", "назови вилку 250-300", examples=("Ваш желаемый доход?",))

    build_answer(_text("Ожидания?"), template, _salary_match(), llm=llm)

    prompt = llm.calls[0][1]["content"]
    assert "назови вилку 250-300" in prompt
    assert "Ваш желаемый доход?" in prompt


def test_build_answer_contextual_pends_below_threshold():
    llm = _LLM({"answer": "возможно", "confidence": 0.85})

    resolved = build_answer(
        _text("Ожидания?"),
        _contextual("salary", "скажи вилку"),
        _salary_match(),
        llm=llm,
        answer_threshold=0.90,
    )

    assert not resolved.resolved and "ниже порога" in resolved.pending_reason


def test_build_answer_contextual_accepts_confidence_at_threshold():
    llm = _LLM({"answer": "точно", "confidence": 0.90})

    resolved = build_answer(
        _text("Ожидания?"),
        _contextual("salary", "скажи вилку"),
        _salary_match(),
        llm=llm,
        answer_threshold=0.90,
    )

    assert resolved.resolved


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer":"x","confidence":NaN}',
        '{"answer":"x","confidence":1.5}',
        '{"answer":"x","confidence":-0.1}',
        "не json вовсе",
        '["не", "объект"]',
        '{"answer":"","confidence":0.99}',
    ],
)
def test_build_answer_contextual_pends_on_malformed_llm_output(raw):
    llm = _LLM(None, raw=raw)

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_pends_on_out_of_range_index():
    llm = _LLM({"answer": "x", "confidence": 0.99, "indices": [7]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_pends_on_multiple_radio_indices():
    llm = _LLM({"answer": "x", "confidence": 0.99, "indices": [0, 1]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_fills_answer_text_from_chosen_options():
    llm = _LLM({"answer": "", "confidence": 0.99, "indices": [1]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert resolved.resolved and resolved.answer == "Удалённо"


def test_build_answer_contextual_pends_when_llm_raises():
    llm = _BoomLLM()

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert llm.calls == 1
    assert not resolved.resolved and "LLM не дал пригодного ответа" in resolved.pending_reason


def test_compliance_contextual_never_reaches_llm():
    llm = _LLM({"answer": "да", "confidence": 1.0})
    template = _contextual("work_permit", "ответь по документам", cluster="compliance")

    resolved = build_answer(_text("Есть ли разрешение?"), template, _compliance_match(), llm=llm)

    assert llm.calls == []
    assert not resolved.resolved


def test_resolved_answer_pending_helper_is_not_resolved():
    assert not ResolvedAnswer.pending("причина").resolved


# --- комплаенс распознаётся по тексту, а не только по кластеру --------------


@pytest.mark.parametrize(
    "text",
    [
        "Готовы предоставить справку об отсутствии судимости?",
        "Есть ли разрешение на работу в РФ?",
        "Ваше гражданство?",
        "Серия и номер паспорта",
        "Ваш СНИЛС",
        "Есть ли вид на жительство?",
        "Действующая медицинская книжка есть?",
        "Есть ли военный билет?",
        "Подтверждаете достоверность указанных сведений?",
        # Живой прогон 2026-08-23: стем «достоверност» не ловил прилагательное
        # «достоверными» — суффикс -ост есть только у существительного. Реальная
        # формулировка встретилась у 3 работодателей, и заверение уходило бы в
        # свободную генерацию вместо комплаенс-гейта.
        "Настоящим подтверждаю, что предоставленные сведения являются "
        "достоверными, полными и точными.",
        "Просим подтвердить, что вы указали в резюме достоверную информацию.",
        "Находитесь ли вы на территории РФ?",
        "Проживаете на территории России?",
    ],
)
def test_compliance_gate_blocks_by_text_without_any_match(text):
    """Гейт обязан срабатывать и когда шаблон не найден вовсе."""
    assert compliance_gate(None, None, text=text) != ""


def test_compliance_gate_by_text_allows_an_explicit_static_answer():
    template = _static("work_permit", "Гражданство РФ", cluster="compliance")

    assert compliance_gate(template, None, text="Ваше гражданство?") == ""


def test_compliance_gate_by_text_rejects_a_contextual_template():
    template = _contextual("work_permit", "ответь", cluster="compliance")

    assert compliance_gate(template, None, text="Ваше гражданство?") != ""


def test_compliance_gate_leaves_ordinary_questions_alone():
    assert compliance_gate(None, None, text="Опишите свой самый сложный проект") == ""


def test_build_answer_blocks_compliance_text_under_a_non_strict_template():
    """Тема определяется текстом даже если шаблон объявлен обычным кластером."""
    resolved = build_answer(
        _text("Ваше гражданство?"),
        _contextual("about_me", "расскажи", cluster="motivation"),
        TemplateMatch("about_me", "motivation", KEYWORD, 0.95),
    )

    assert not resolved.resolved


@pytest.mark.parametrize(
    "text",
    [
        "Подскажите, вы на данный момент проживаете в РФ?",
        "Территориально проживаете в РФ? В каком городе?",
        "Вы проживаете в России?",
    ],
)
def test_residency_wording_is_answered_as_ordinary_location(text):
    """«Проживаете в РФ?» — вопрос о местоположении, а не о правовом статусе.

    Живой прогон 2026-08-23: этой формулировкой работодатель выясняет, откуда
    кандидат работает, и ответ на неё уже лежит в шаблоне ``location`` — каким
    бы он ни был у конкретного пользователя («Воронеж», «Таиланд»). Попытка
    считать её комплаенсом уводила в очередь вопрос, на который есть готовый
    ответ, и заодно блокировала обычные вопросы про российский рынок.
    Юридические формулировки закрыты отдельно, см.
    ``test_compliance_gate_blocks_by_text_without_any_match``.
    """
    match = resolve_template(text, confirmed={})
    resolved = build_answer(_text(text), _static("location", "Таиланд"), match)

    assert match is not None and match.template == "location"
    assert resolved.resolved
    assert resolved.answer == "Таиланд"


def test_compliance_question_is_not_answered_by_a_guessed_template():
    """Комплаенс не отвечается шаблоном, который подобрала эвристика.

    #482: «ни ключевые слова, ни LLM» не отвечают на комплаенс. Проверки
    «шаблон static и непуст» для этого мало — гейт обязан смотреть, ЧЬИМ
    решением выбран сам шаблон, иначе значение, сохранённое под другую тему,
    засчитывается как явный комплаенс-ответ.
    """
    match = TemplateMatch("location", "conditions", KEYWORD, 0.95)
    resolved = build_answer(_text("Ваше гражданство?"), _static("location", "Таиланд"), match)

    assert not resolved.resolved
    assert not resolved.answer


def test_confirmed_phrase_still_answers_a_compliance_question():
    """Подтверждённая человеком формулировка — не догадка, гейт её пропускает."""
    text = "Ваше гражданство?"
    match = match_phrase(text, {normalize(text): "citizenship"})
    resolved = build_answer(_text(text), _static("citizenship", "РФ", cluster="compliance"), match)

    assert match is not None and match.source == PHRASE
    assert resolved.resolved
    assert resolved.answer == "РФ"


def test_compliance_question_is_answered_by_explicit_static_template():
    """Явно объявленный комплаенс-шаблон отвечает — гейт не глухой."""
    resolved = build_answer(
        _text("Ваше гражданство?"),
        _static("citizenship", "РФ", cluster="compliance"),
        TemplateMatch("citizenship", "compliance", KEYWORD, 0.95),
    )

    assert resolved.resolved
    assert resolved.answer == "РФ"


# --- склонение и словоформы (регресс: фиксированные формы промахивались) ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # salary во всех падежах и синонимах
        ("Ваши зарплатные ожидания?", "salary"),
        ("Укажите желаемую зарплату", "salary"),
        ("Зарплата?", "salary"),
        ("Какая зарплата вас интересует?", "salary"),
        ("Ваши пожелания по окладу", "salary"),
        ("Желаемый оклад", "salary"),
        ("Заработная плата?", "salary"),
        ("уровень доходов", "salary"),
        ("Уровень оплаты труда", "salary"),
        ("Сколько вы хотите зарабатывать?", "salary"),
        # location
        ("В каком городе вы проживаете?", "location"),
        ("Город проживания", "location"),
        ("Где вы живёте?", "location"),
        ("Где вы живете?", "location"),
        ("Страна проживания?", "location"),
        ("Ваш город?", "location"),
        # Живой прогон 2026-08-23: реальные формулировки, промахивавшиеся мимо
        # шаблона и уходившие в очередь как незнакомые.
        ("Из какого города вы планируете работать?", "location"),
        # desired_role
        ("Желаемая должность?", "desired_role"),
        ("Укажите желаемую роль", "desired_role"),
        ("Какая роль вам интересна?", "desired_role"),
        ("Какие задачи вам интересны?", "desired_role"),
        ("Чем хотите заниматься?", "desired_role"),
        # business_segments
        ("С какими сегментами бизнеса вы работали?", "business_segments"),
        ("Сегмент бизнеса?", "business_segments"),
        ("В каких сферах у вас опыт?", "business_segments"),
        ("Работали ли вы с B2B или B2C?", "business_segments"),
    ],
)
def test_keyword_matching_survives_russian_inflection(text, expected):
    """Русский вопрос склоняется: фиксированные словоформы промахивались на
    большинстве реальных формулировок, и вопрос молча уходил в очередь."""
    match = match_keyword(text)

    assert match is not None, f"не распознано: {text}"
    assert match.template == expected


def test_seed_patterns_do_not_collide():
    """Признак, попавший в два шаблона, снимает сопоставление вовсе (fail-closed).

    Безвредно по последствиям, но тихо ломает автоматизацию: шаблон
    перестаёт срабатывать вообще. Страж на случай добавления новых стемов.
    """
    from hhru_bot.external_forms.detect import normalize
    from hhru_bot.questionnaires.templates import SEED_TEMPLATES

    canonical = {
        "salary": ("зарплатные ожидания", "желаемая зарплата", "оклад", "уровень дохода"),
        "location": ("в каком городе", "город проживания", "страна проживания", "ваш город"),
        "desired_role": (
            "желаемая должность",
            "какие задачи вам интересны",
            "чем хотите заниматься",
        ),
        "business_segments": ("сегменты бизнеса", "в каких сферах", "с какими нишами"),
    }
    for owner, texts in canonical.items():
        for text in texts:
            hits = [seed.name for seed in SEED_TEMPLATES if seed.matches(normalize(text))]
            assert hits == [owner], f"{text!r} задевает {hits}, ожидался только {owner}"


@pytest.mark.parametrize(
    "text",
    [
        "Опишите свой самый сложный проект",
        "Ваши зарплатные ожидания?",
        "В каком городе вы живёте?",
        "Есть ли опыт работы с юридическими лицами?",
        "Знакомы ли вы с инновациями в отрасли?",
        "Какие задачи вам интересны?",
    ],
)
def test_compliance_pattern_does_not_catch_ordinary_questions(text):
    """Ложное срабатывание безвредно (вопрос уйдёт в очередь), но глушит
    автоматизацию — обычные вопросы паттерн задевать не должен."""
    from hhru_bot.questionnaires.templates import is_compliance_text

    assert is_compliance_text(text) is False


# --- markdown-обёртка вокруг JSON ------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer":"да","confidence":0.95}',
        '```json\n{"answer":"да","confidence":0.95}\n```',
        '```\n{"answer":"да","confidence":0.95}\n```',
        '  {"answer":"да","confidence":0.95}  ',
    ],
)
def test_llm_payload_survives_a_markdown_code_fence(raw):
    """Модели добавляют ограждение вопреки инструкции «только JSON»; без снятия
    валидный ответ отбрасывался бы как испорченный."""
    from hhru_bot.questionnaires.resolver import parse_llm_payload

    payload, confidence = parse_llm_payload(raw)

    assert payload["answer"] == "да"
    assert confidence == pytest.approx(0.95)


def test_fenced_answer_is_accepted_end_to_end():
    llm = _LLM(None, raw='```json\n{"answer":"250-300","confidence":0.97}\n```')

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert resolved.resolved and resolved.answer == "250-300"


@pytest.mark.parametrize(
    "text",
    [
        # «оклад» без границы слова ловил «доклад» — вопрос о выступлениях
        # получал бы в ответ зарплатные ожидания.
        "Готовы ли вы выступать с докладами?",
        "Есть ли опыт публичных докладов?",
        "Опишите доклад на конференции",
        # «на какую сумму» ловило вопрос о сделках, а не о зарплате.
        "На какую сумму был крупнейший контракт?",
        # «какие задачи» без уточнения — вопрос о ПРОШЛОМ опыте.
        "Какие задачи вы решали?",
        "Какие задачи входили в ваши обязанности?",
        # обычные вопросы, не относящиеся ни к одному seed-полю
        "Опишите самый сложный проект",
        "Есть ли опыт управления командой?",
        "Ваш стаж работы?",
        "Почему вы уходите с текущего места?",
        # «находитесь» без уточнения места — вопрос о причинах поиска работы,
        # а не о локации; стем location не должен его задевать.
        "Почему сейчас находитесь в поиске работы?",
    ],
)
def test_seed_patterns_do_not_match_unrelated_questions(text):
    """Негативный страж: одно совпадение = уверенный ответ, и ложное
    срабатывание отправляет работодателю ответ не на тот вопрос. Позитивных
    проверок для этого мало — они не видят лишних попаданий."""
    assert match_keyword(text) is None


# --- тема против намерения (#490) -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Есть ли опыт расчета зарплаты сотрудников?",
        "Есть ли опыт начисления заработной платы?",
        "Вы занимались расчетом окладов?",
        # ё-написание отдельным случаем: normalize делает casefold и схлопывает
        # пробелы, но ё->е НЕ сворачивает, поэтому потерянный класс [её] в
        # контр-признаке не поймает ни один другой тест этого блока.
        "Автоматизировали ли вы расчёт зарплаты?",
        # Вставные предлоги: «опыт В расчете», «опыт ПО начислению».
        "Есть ли у вас опыт в расчете заработной платы?",
        "Какой опыт по начислению зарплаты?",
        "Опыт начисления окладов?",
        # Глагольные формы: тот же вопрос об опыте, но одни лишь именные стемы
        # («начислен») их пропускали.
        "Приходилось ли начислять зарплату?",
        "Вы считали зарплату сотрудникам?",
        "Вели ли вы расчет заработной платы?",
        "Вы начисляете зарплату сотрудникам?",
        "Кто у вас начислял зарплату?",
        "Умеете ли вы рассчитывать зарплату?",
        "Приходилось ли вам считать зарплату сотрудникам?",
        # Слова между действием и деньгами.
        "Знаком ли вам процесс начисления премий и окладов?",
        "Вы занимались начислением окладов?",
        # Приставка «пере-»: \b требует, чтобы слово начиналось со стема, и без
        # явного разрешения «перерасчёт» проходил бы мимо контр-признака.
        "Есть ли опыт перерасчета заработной платы?",
        "Опыт перерасчёта зарплаты?",
        "Вы занимались перерасчетом зарплаты?",
        # «Зарплатный проект»/«ведомость» — банковская услуга и документ,
        # а не мои ожидания.
        "Есть опыт работы с зарплатными проектами?",
        "Опыт формирования зарплатной ведомости?",
    ],
)
def test_match_keyword_skips_salary_experience_question(text):
    """#490: тема «зарплата» ещё не значит «спрашивают мои ожидания».

    До фикса каждая из этих формулировок получала уверенный salary (0.95) и
    отправила бы работодателю сумму вместо ответа об опыте. Вопрос уходит в
    очередь — цена ошибки асимметрична: неотвеченный вопрос стоит одной
    вакансии, неверный ответ уходит работодателю навсегда.
    """
    assert match_keyword(text) is None


#: Все 17 salary-вопросов живого корпуса (146 уникальных формулировок,
#: собранных ``probe --questionnaires-only``, выгрузка 2026-08-23).
#: Строковые литералы, а не чтение history.db: вся ``data/`` в .gitignore, в
#: CI её нет — тест, читающий базу, там бы просто не работал.
_CORPUS_SALARY_QUESTIONS = (
    "Ваш текущий уровень дохода — оклад и совокупная часть; "
    "Ваши ожидания по заработной плате — минимальный и комфортный уровень.",
    "Ваши зарплатные ожидания",
    "Желаемый размер заработной платы?",
    "Желаемый уровень заработной платы",
    "Какие у вас ожидания по заработной плате (net/на руки)?",
    "Каковы ваши зарплатные ожидания?",
    "Какой минимальный и комфортный уровни заработной платы вы рассматриваете?",
    "Какой оклад на руки вам интересен?",
    "Какой уровень заработной платы сейчас рассматриваете (на руки)? "
    "Можно указать диапазон - например, в формате минимальная и комфортная планка.",
    "Какой уровень оплаты рассматриваете (в рублях)?",
    "На какую заработную плату Вы рассчитываете после первого месяца обучения?",
    "Подскажите ваши зарплатные ожидания?",
    "Твои зарплатные ожидания?",
    "Укажите ваши зарплатные ожидания (сумма после налогов)",
    "Укажите пожалуйста Ваши ожидания по зарплате",
    "Укажите, пожалуйста, уровень заработной платы, который Вы рассматриваете.",
    "Что рассматриваете в окладе в месяц?",
)


@pytest.mark.parametrize("text", _CORPUS_SALARY_QUESTIONS)
def test_salary_corpus_still_matches_after_intent_filter(text):
    """Отдельная проверка по корпусу, которую требует #490.

    Контр-признаки задевают весь seed salary сразу, поэтому недостаточно
    убедиться, что негативы отсеяны: все реально встречавшиеся формулировки
    обязаны продолжать отвечаться. Здесь же закреплено, что ``\\bрасч`` не
    цепляет «рассчитываете» и «рассматриваете» — двойное «с» рвёт совпадение,
    и без этих строк регресс был бы незаметен.
    """
    match = match_keyword(text)

    assert match is not None, f"корпусный вопрос перестал распознаваться: {text}"
    assert match.template == "salary"


@pytest.mark.parametrize(
    "text",
    [
        "Рассчитываете на какой оклад?",
        "На какую зарплату рассчитываете?",
        "Сколько рассчитываете получать?",
    ],
)
def test_expectation_via_rasschityvat_is_not_suppressed(text):
    """«Рассчитывать НА» — это ожидания, а не расчёт зарплаты (#490).

    Стем выглядит однокоренным с «расчёт», и добавить его в контр-признаки
    заманчиво: тогда подавились бы ровно те вопросы, ради которых шаблон
    существует. Тест держит эту границу — без него расширение контр-признака
    «заодно на однокоренные» прошло бы незамеченным.
    """
    match = match_keyword(text)

    assert match is not None and match.template == "salary"


def test_salary_abbreviation_question_stays_unmatched():
    """Вопрос про «ЗП вилку» по-прежнему уходит в очередь (решение #487).

    Аббревиатура не заведена намеренно: ровно одно вхождение на 146 собранных
    вопросов, снимается одной командой ``questionnaire learn``. Тест стоит
    здесь, чтобы признак не «починили» заодно с #490.
    """
    text = "⁠От какой ЗП вилки на данный момент готовы рассматривать предложения для себя?"

    assert match_keyword(text) is None


def test_marketing_question_about_dropoff_is_not_salary():
    """«Пользователи не доходят до конца марафона» — не про доход.

    Реальный вопрос корпуса: стем ``доход`` живёт внутри «доходят», и поймать
    его означало бы ответить маркетинговому вопросу зарплатной вилкой.
    """
    text = (
        "Маркетолог говорит, что пользователи не доходят до конца марафона. "
        "Как бы вы технически решили эту проблему?"
    )

    assert match_keyword(text) is None


def test_suppressed_salary_leaves_composite_question_to_the_other_template():
    """Принятый компромисс #490, закреплённый по ФАКТИЧЕСКОМУ поведению.

    Подавление живёт внутри ``SeedTemplate.matches``, поэтому оно fail-closed
    только пока salary — единственный хит. Составной вопрос задевал два
    шаблона и уходил в очередь; после подавления salary остаётся один хит, и
    вопрос отвечается уверенно. Компромисс принят осознанно: на оставшуюся
    половину вопроса ответ действительно есть. Тест существует, чтобы смена
    поведения была задокументирована, а не обнаружена в боевом прогоне.
    """
    match = match_keyword("Есть ли опыт расчёта зарплаты? В каком городе вы живёте?")

    assert match is not None and match.template == "location"


def test_excludes_suppress_only_their_own_seed():
    """Контр-признаки salary не влияют на чужие шаблоны.

    Проверяется поведение, а не список seed'ов с ``excludes``: заводить
    контр-признаки другому шаблону — законный шаг, и тест не должен падать
    просто из-за этого. Падать он обязан, если подавление протечёт наружу.
    """
    from hhru_bot.questionnaires.templates import SEED_TEMPLATES

    salary = next(seed for seed in SEED_TEMPLATES if seed.name == "salary")
    others = [seed for seed in SEED_TEMPLATES if seed.name != "salary"]

    # Текст, который salary обязан подавить...
    assert not salary.matches(normalize("Есть ли опыт расчета зарплаты сотрудников?"))
    # ...остальные шаблоны и так не трогают, но по своим признакам, а не по
    # чужим контр-признакам: вопрос про город рядом с зарплатой отвечается.
    location = next(seed for seed in others if seed.name == "location")
    assert location.matches(normalize("Есть ли опыт расчета зарплаты? В каком городе вы живёте?"))
