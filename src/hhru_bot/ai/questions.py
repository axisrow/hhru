"""LLM-assisted answers for confirmed hh.ru response-form questions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..selector_groups import apply_form

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

    from ..config_sections.ai_profile import AIProfile
    from .llm_client import LLMClient

logger = logging.getLogger("hhru_bot.ai.questions")

CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class Question:
    body_index: int
    text: str
    kind: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerProposal:
    question: Question
    answer: str
    confidence: float
    option_indices: tuple[int, ...] = ()

    @property
    def low_confidence(self) -> bool:
        return self.confidence < CONFIDENCE_THRESHOLD


def _control_text(control: Locator) -> str:
    """Read the visible label without relying on an unconfirmed selector."""
    try:
        return str(
            control.evaluate(
                """el => {
                    const label = el.closest('label');
                    return (label || el.parentElement || el).innerText || el.value || '';
                }"""
            )
        ).strip()
    except Exception:  # noqa: BLE001 - a missing label is still diagnosable
        return ""


def extract_questions(page: Page) -> list[Question]:
    """Extract questions using selectors confirmed by the live probe (#257)."""
    bodies = page.locator(apply_form.APPLY_QUESTION_FORM_BODY)
    questions: list[Question] = []
    for body_index in range(bodies.count()):
        body = bodies.nth(body_index)
        text = body.locator(apply_form.APPLY_QUESTION_TEXT).first.inner_text().strip()
        radios = body.locator("input[type='radio']")
        checkboxes = body.locator("input[type='checkbox']")
        textareas = body.locator("textarea")
        if radios.count() or checkboxes.count():
            controls = radios if radios.count() else checkboxes
            options = tuple(_control_text(controls.nth(i)) for i in range(controls.count()))
            # M7 cycle-review #373: _control_text() fails closed to "" when the
            # label lookup errors or is missing entirely. But when a radio has
            # no <label> wrapper (unconfirmed Bloko markup — #97 selectors are
            # confirmed for task-body/task-question, NOT for label structure),
            # `parentElement.innerText` falls back to task-body's full text,
            # verified against live Chromium: every option in that body then
            # gets the SAME (non-empty) text — the question's own text, not a
            # per-option label. A blank OR non-unique option set both mean the
            # LLM would answer against labels it cannot actually distinguish —
            # "wrong answer worse than skip" (#97). Dropping the question here
            # (not appending it) makes it absent from extracted, which
            # pipeline.py's has_questions/extracted mismatch check already
            # treats as a fail-closed skip.
            if any(not option for option in options) or (
                len(options) > 1 and len(set(options)) != len(options)
            ):
                logger.warning(
                    "Анкета: не распознаны отдельные тексты опций в вопросе %r — вопрос пропущен",
                    text,
                )
                continue
            questions.append(Question(body_index, text, "choice", options))
        elif textareas.count():
            questions.append(Question(body_index, text, "text"))
    return questions


def _profile_context(profile: AIProfile | None) -> str:
    if profile is None:
        return ""
    return "\n".join(
        part
        for part in (
            f"О кандидате: {profile.summary}" if profile.summary else "",
            f"Навыки: {', '.join(profile.skills)}" if profile.skills else "",
            f"Желаемая роль: {profile.desired_role}" if profile.desired_role else "",
        )
        if part
    )


def _prompt(question: Question, profile: AIProfile | None) -> list[dict[str, str]]:
    system = (
        "Ты предлагаешь ответы на вопросы анкеты для отклика на hh.ru. "
        "Не выдумывай факты. Ответь только JSON без markdown: "
        '{"answer":"...","confidence":0.0,"indices":[0]}. '
        "confidence должен отражать уверенность от 0 до 1."
    )
    if question.kind == "choice":
        task = "Выбери один или несколько наиболее подходящих вариантов по их индексам (с нуля)."
        options = "\n".join(f"{i}: {value}" for i, value in enumerate(question.options))
        task += f"\nВарианты:\n{options}"
    else:
        task = "Сгенерируй краткий правдивый ответ на русском языке."
    user = f"Вопрос: {question.text}\n{task}"
    context = _profile_context(profile)
    if context:
        user += f"\n\n{context}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class AIQuestionAnswerer:
    def __init__(self, llm: LLMClient, profile: AIProfile | None = None):
        self._llm = llm
        self._profile = profile

    def propose(self, question: Question) -> AnswerProposal:
        try:
            response = self._llm.chat(_prompt(question, self._profile), temperature=0.2)
            payload: Any = json.loads((response.content or "").strip())
            confidence = float(payload.get("confidence", 0))
            indices = tuple(int(i) for i in payload.get("indices", []))
            answer = str(payload.get("answer", "")).strip()
            if question.kind == "choice" and not indices:
                raise ValueError("LLM returned no choice index")
            invalid_index = any(i < 0 or i >= len(question.options) for i in indices)
            if question.kind == "choice" and invalid_index:
                raise ValueError("LLM returned an out-of-range choice index")
            if question.kind == "text" and not answer:
                raise ValueError("LLM returned an empty text answer")
            return AnswerProposal(question, answer, confidence, indices)
        except Exception as exc:  # noqa: BLE001 - malformed AI output is low confidence
            logger.warning("Не удалось получить ответ на вопрос: %s", exc)
            return AnswerProposal(question, "", 0.0)

    def propose_all(self, questions: list[Question]) -> list[AnswerProposal]:
        return [self.propose(question) for question in questions]

    @staticmethod
    def apply(page: Page, proposals: list[AnswerProposal]) -> list[AnswerProposal]:
        """Fill only high-confidence proposals. Low-confidence questions stay blank."""
        bodies = page.locator(apply_form.APPLY_QUESTION_FORM_BODY)
        for proposal in proposals:
            if proposal.low_confidence:
                continue
            body = bodies.nth(proposal.question.body_index)
            if proposal.question.kind == "text":
                body.locator("textarea").first.fill(proposal.answer)
                continue
            radios = body.locator("input[type='radio']")
            controls = radios if radios.count() else body.locator("input[type='checkbox']")
            for index in proposal.option_indices:
                controls.nth(index).check()
        return [proposal for proposal in proposals if proposal.low_confidence]
