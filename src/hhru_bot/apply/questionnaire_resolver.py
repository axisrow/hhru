"""Keyword resolver + template model for questionnaire answers (issue #482).

Owner: #482. Independent of AI (issue decision "Отдельный resolver,
независимый от LLM") -- ``resolve_by_keyword`` and the static branch of
``resolve_answer`` never touch an LLM. The LLM is only a fallback for
contextual templates, wired in by the caller (``commands/_common.py``), not
a hard dependency of this module.

Two template modes (issue decision "Static и contextual-шаблоны"):
  - ``static``: a fixed, previously confirmed answer looked up from account/
    resume-scoped storage (``History.get_template_answers``). No LLM call.
  - ``contextual``: an instruction plus confirmed examples are rendered into
    a prompt and answered by an LLM. Without an LLM, a contextual template
    cannot be answered at all -- this module returns ``None`` rather than a
    fabricated low-confidence guess (there is nothing to be unconfident
    about: no attempt was made).

Documents/compliance fields (issue decision "Документы и комплаенс отвечаются
только явным сохраненным значением") reuse the same denylist as the existing
external-forms LLM matcher (``external_forms.detect.is_denied_field``) so the
two code paths never diverge on what is excluded from auto-answering.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..external_forms.detect import is_denied_field, normalize

if TYPE_CHECKING:
    from ..ai.llm_client import LLMClient

logger = logging.getLogger("hhru_bot.apply.questionnaire_resolver")

_STATIC = "static"
_CONTEXTUAL = "contextual"
VALID_MODES = (_STATIC, _CONTEXTUAL)

DEFAULT_ANSWER_THRESHOLD = 0.90


@dataclass(frozen=True)
class Template:
    """A learnable answer template (issue #482).

    ``answer`` is intentionally NOT stored here: static answers are
    account/resume-scoped values looked up separately (``resume_answers``/
    ``account_answers`` passed to ``resolve_answer``), because the same
    template can carry a different value per resume ("Ответы общие для
    аккаунта с resume overrides").
    """

    name: str
    mode: str
    instruction: str | None = None
    examples: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"unknown template mode: {self.mode!r}")


@dataclass(frozen=True)
class TemplateAnswerProposal:
    """A resolved answer produced by the keyword resolver / template chain.

    Deliberately mirrors ``ai.questions.AnswerProposal``'s shape (``answer``,
    ``confidence``, ``low_confidence``) so callers that already know that
    contract (``pipeline.py``) don't need a second one -- the two are
    unified into a single object by the caller in ``commands/_common.py``,
    not by this module.

    ``threshold`` is carried on the instance (not a module constant) because
    it comes from ``questionnaires.llm_answer_threshold`` in config and a
    static answer (always confidence 1.0) must never be gated by it.
    """

    template: str
    answer: str
    confidence: float
    answer_source: str = "template"
    threshold: float = DEFAULT_ANSWER_THRESHOLD

    @property
    def low_confidence(self) -> bool:
        return self.confidence < self.threshold


def resolve_by_keyword(question_text: str, confirmed_matches: dict[str, str]) -> str | None:
    """Return the template name confirmed for this question text, or ``None``.

    Pure function: no LLM, no browser -- issue #482's "Keyword resolver
    работает без AI-зависимости". ``confirmed_matches`` keys are expected to
    already be ``normalize()``d (as stored by
    ``History.get_confirmed_matches``); the question text is normalized here
    so callers can pass the raw label straight from ``Question.text``.
    """
    if not confirmed_matches:
        return None
    return confirmed_matches.get(normalize(question_text))


def is_denied_answer_field(text: str) -> bool:
    """Whether *text* (a template name or question label) is a compliance field."""
    return is_denied_field(text)


def _resolve_static(
    template: Template,
    resume_answers: dict[str, str],
    account_answers: dict[str, str],
) -> TemplateAnswerProposal | None:
    answer = resume_answers.get(template.name) or account_answers.get(template.name)
    if not answer:
        return None
    return TemplateAnswerProposal(template=template.name, answer=answer, confidence=1.0)


def _contextual_prompt(template: Template, question_text: str) -> list[dict[str, str]]:
    system = (
        "Ты отвечаешь на вопрос анкеты отклика на hh.ru, используя заранее "
        "заданную инструкцию и примеры прошлых подтверждённых ответов. "
        "Не выдумывай факты сверх инструкции. Ответь только JSON без markdown: "
        '{"answer":"...","confidence":0.0}. confidence — уверенность от 0 до 1.'
    )
    user = f"Инструкция: {template.instruction}\nВопрос анкеты: {question_text}"
    if template.examples:
        examples = "\n".join(f"- {example}" for example in template.examples)
        user += f"\n\nПодтверждённые примеры прошлых ответов:\n{examples}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _resolve_contextual(
    template: Template,
    question_text: str,
    llm: LLMClient | None,
) -> TemplateAnswerProposal | None:
    if llm is None:
        # No attempt made -- nothing to be unconfident about. Distinct from a
        # malformed/low-confidence LLM response, which DOES return a proposal
        # below so the caller can audit/pending it (#482: "первое LLM-
        # сопоставление требует подтверждения", unresolved questions must be
        # traceable, not silently dropped).
        return None
    try:
        response = llm.chat(_contextual_prompt(template, question_text), temperature=0.2)
        payload: Any = json.loads((response.content or "").strip())
        confidence = float(payload.get("confidence", 0))
        if not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
            raise ValueError(f"LLM returned a non-finite/out-of-range confidence: {confidence!r}")
        answer = str(payload.get("answer", "")).strip()
    except Exception as exc:  # noqa: BLE001 - malformed AI output is low confidence
        logger.warning(
            "Не удалось получить контекстный ответ по шаблону '%s': %s", template.name, exc
        )
        return TemplateAnswerProposal(template=template.name, answer="", confidence=0.0)
    return TemplateAnswerProposal(template=template.name, answer=answer, confidence=confidence)


def resolve_answer(
    template: Template,
    *,
    resume_answers: dict[str, str],
    account_answers: dict[str, str],
    llm: LLMClient | None = None,
    answer_threshold: float = DEFAULT_ANSWER_THRESHOLD,
    question_text: str = "",
) -> TemplateAnswerProposal | None:
    """Resolve one template into an answer proposal, or ``None`` if unresolved.

    Fail-closed on compliance fields regardless of mode or stored value
    (issue #482: "Документы и комплаенс отвечаются только явным сохраненным
    значением" -- even an explicitly saved value for a denied template name
    is refused here, matching the existing ``match_answer_llm`` boundary).

    ``answer_threshold`` only affects whether the returned proposal's
    ``low_confidence`` reads true for a *contextual* answer; a static
    lookup is always confidence 1.0 (either the value is known or it is
    not -- there is no partial confidence for an exact stored fact).
    """
    if is_denied_answer_field(template.name):
        return None
    if template.mode == _STATIC:
        # Static answers are exact stored facts: always full confidence,
        # never gated by answer_threshold (there is no partial confidence
        # for "the value is known").
        return _resolve_static(template, resume_answers, account_answers)
    proposal = _resolve_contextual(template, question_text or template.name, llm)
    if proposal is None:
        return None
    return TemplateAnswerProposal(
        template=proposal.template,
        answer=proposal.answer,
        confidence=proposal.confidence,
        answer_source=proposal.answer_source,
        threshold=answer_threshold,
    )


def suggest_template_llm(
    question_text: str,
    template_names: list[str],
    llm: LLMClient | None,
    *,
    threshold: float = DEFAULT_ANSWER_THRESHOLD,
) -> tuple[str, float] | None:
    """Suggest an EXISTING template name for an unresolved question (``--learn-questionnaires``).

    A pure classifier, same safety shape as ``external_forms.detect.match_answer_llm``:
    the model selects a name from ``template_names`` (never invents one, never
    sees/returns an answer value) and must clear ``threshold``. The result is
    a *suggestion* only -- issue #482: "Первое LLM-сопоставление требует
    подтверждения пользователя" -- callers must persist it to
    ``questionnaire_pending.suggested_template`` and route it through
    ``History.confirm_match`` (via ``questionnaire learn``) before
    ``resolve_by_keyword`` can ever use it.
    """
    if not template_names or llm is None:
        return None
    prompt = (
        "Сопоставь вопрос анкеты с одним из существующих шаблонов ответа по имени. "
        "Не придумывай имя. Верни только JSON вида "
        '{"template": "точное имя из списка или null", "confidence": 0.0}. '
        "Выбирай шаблон только если он действительно отвечает на вопрос; иначе template=null.\n"
        + json.dumps(
            {"question": question_text, "known_templates": sorted(template_names)},
            ensure_ascii=False,
        )
    )
    try:
        response = llm.chat([{"role": "user", "content": prompt}], temperature=0)
        payload: Any = json.loads((response.content or "").strip())
        name = payload.get("template") if isinstance(payload, dict) else None
        confidence = payload.get("confidence") if isinstance(payload, dict) else None
        if (
            isinstance(name, str)
            and name in template_names
            and not is_denied_answer_field(name)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
            and confidence >= threshold
        ):
            return name, float(confidence)
    except Exception as exc:  # noqa: BLE001 - malformed/failed LLM -> no suggestion
        logger.warning("Не удалось предложить шаблон для вопроса: %s", exc)
        return None
    return None
