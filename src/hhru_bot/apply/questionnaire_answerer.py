"""Resolver-chain object plugged into ``pipeline.py`` (issue #482).

``pipeline.py`` calls ``ctx.question_answerer.propose_all(extracted)`` /
``ctx.question_answerer.apply(page, proposals)`` and nothing else
(``pipeline.py`` stays a thin sequencer per CLAUDE.md -- see #482 plan).
``HybridQuestionAnswerer`` exposes that exact surface so no pipeline change
is needed; it decides internally which stage answers a given question:

  1. keyword resolver (``questionnaire_resolver.resolve_by_keyword`` +
     ``resolve_answer``) against confirmed templates -- no AI dependency.
  2. the existing ``AIQuestionAnswerer`` (profile/LLM), only if the caller
     supplied one (``ai.answer_questions: true``).
  3. neither resolved it -> a low-confidence ``AnswerProposal`` so the
     existing skip/pending machinery in ``pipeline.py``/``_common.py``
     handles it the same way an unanswerable LLM question already does.

Built by ``commands/_common.py::_build_question_answerer`` only when
``questionnaires.enabled`` is set; when only ``ai.answer_questions`` is set
(no ``questionnaires`` section), ``_common.py`` keeps returning the plain
``AIQuestionAnswerer`` directly -- this wrapper is not involved and behaviour
stays byte-identical to before #482.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..ai.questions import AIQuestionAnswerer, AnswerProposal, Question
from .questionnaire_resolver import resolve_answer, resolve_by_keyword, suggest_template_llm

if TYPE_CHECKING:
    from playwright.sync_api import Page


class HybridQuestionAnswerer:
    def __init__(
        self,
        *,
        history,
        resume_id: str,
        llm_answerer: AIQuestionAnswerer | None,
        llm: object | None = None,
        answer_threshold: float = 0.90,
        learn_questionnaires: bool = False,
    ):
        """``history`` is a ``History`` instance (typed loosely to avoid a
        pipeline.py <-> history.py import cycle -- see ``History.get_template``'s
        own lazy import for the same reason). ``llm`` is the raw ``LLMClient``
        used for contextual-template answers; it is separate from
        ``llm_answerer`` (the full ``AIQuestionAnswerer``, used only as the
        profile/LLM fallback stage) because a contextual template's prompt
        has nothing to do with ``AIQuestionAnswerer``'s own prompt/profile.

        ``learn_questionnaires`` gates ``suggest_template`` (``--learn-questionnaires``,
        issue #482). Deliberately does NOT gate anything else here: it never
        prompts interactively (there is no page/dry_run/force visibility at
        this layer, and pipeline.py's post-click "grey zone" must never block
        on input()) -- it only enriches what ``pipeline.py`` hands to
        ``History.enqueue_pending`` for later ``questionnaire learn`` review.
        """
        self._history = history
        self._resume_id = resume_id
        self._llm_answerer = llm_answerer
        self._llm = llm
        self._learn_questionnaires = learn_questionnaires
        self._answer_threshold = answer_threshold

    def _resolve_via_keyword(self, question: Question) -> AnswerProposal | None:
        confirmed = self._history.get_confirmed_matches()
        template_name = resolve_by_keyword(question.text, confirmed)
        if template_name is None:
            return None
        template = self._history.get_template(template_name)
        if template is None:
            # Confirmed match points at a deleted template (questionnaire
            # unset) -- fail-closed to "not resolved" rather than crashing.
            return None
        answers = self._history.get_template_answers(template_name)
        resume_answers = (
            {template_name: answers["resume"][self._resume_id]}
            if self._resume_id in answers["resume"]
            else {}
        )
        account_answers = {template_name: answers["account"]} if answers["account"] else {}
        proposal = resolve_answer(
            template,
            resume_answers=resume_answers,
            account_answers=account_answers,
            llm=self._llm,
            answer_threshold=self._answer_threshold,
            question_text=question.text,
        )
        if proposal is None:
            return None
        # #482: "варианты сопоставляются по тексту, не по сохраненным
        # индексам" -- reuse the existing text->index matcher rather than
        # duplicating it.
        indices = AIQuestionAnswerer._choice_indices(question, proposal.answer)
        if question.kind == "choice" and not indices:
            # Stored answer text doesn't match any visible option verbatim --
            # not a safe fill; let the caller's low-confidence/pending path
            # take over instead of silently picking nothing.
            return None
        return AnswerProposal(
            question,
            proposal.answer,
            proposal.confidence,
            indices,
            proposal.answer_source,
            proposal.template,
        )

    def propose(self, question: Question) -> AnswerProposal:
        resolved = self._resolve_via_keyword(question)
        if resolved is not None and not resolved.low_confidence:
            return resolved
        if self._llm_answerer is not None:
            return self._llm_answerer.propose(question)
        if resolved is not None:
            return resolved
        return AnswerProposal(question, "", 0.0)

    def propose_all(self, questions: list[Question]) -> list[AnswerProposal]:
        return [self.propose(question) for question in questions]

    def apply(self, page: Page, proposals: list[AnswerProposal]) -> list[AnswerProposal]:
        """Delegate DOM filling to the already-tested ``AIQuestionAnswerer.apply``.

        Filling logic (radio/checkbox/textarea, low-confidence skip) has
        nothing resolver-specific about it once an ``AnswerProposal`` exists
        -- reusing it avoids duplicating that DOM-interaction code.
        """
        return AIQuestionAnswerer.apply(page, proposals)

    def suggest_template(self, question: Question) -> tuple[str, float] | None:
        """Suggest an existing template for an unresolved question (``--learn-questionnaires``).

        Returns ``None`` when the flag is off (default) -- opt-in, and this
        never auto-applies the suggestion: the caller (``pipeline.py``) only
        stores it on the pending-queue row for later confirmation via
        ``questionnaire learn`` (issue #482: "Первое LLM-сопоставление
        требует подтверждения пользователя").
        """
        if not self._learn_questionnaires or self._llm is None:
            return None
        names = [template.name for template in self._history.list_templates()]
        return suggest_template_llm(question.text, names, self._llm)
