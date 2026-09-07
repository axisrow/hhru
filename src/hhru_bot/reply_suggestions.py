"""Context-bounded recruiter reply suggestions (issue #593).

This module deliberately accepts one inbound message and one unambiguous vacancy.
It does not know about neighbouring chats or configuration serialization.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ai.prompt_safety import UNTRUSTED_DATA_INSTRUCTION, wrap_untrusted


class AmbiguousReplyContext(ValueError):
    """The topic cannot be mapped to exactly one vacancy/resume."""


@dataclass(frozen=True)
class ReplyContext:
    topic: str
    inbound_marker: str
    inbound_text: str
    vacancy_id: str
    vacancy_title: str
    employer: str
    resume_id: str | None = None


def validate_context(contexts: list[ReplyContext]) -> ReplyContext:
    """Fail closed unless there is exactly one mapping."""
    if len(contexts) != 1:
        raise AmbiguousReplyContext("ambiguous topic/vacancy/resume mapping")
    item = contexts[0]
    if not all((item.topic, item.inbound_marker, item.inbound_text.strip(), item.vacancy_id)):
        raise AmbiguousReplyContext("incomplete topic/vacancy/inbound mapping")
    return item


def build_prompt(context: ReplyContext) -> list[dict[str, str]]:
    """Build an allow-listed prompt: exactly one message and vacancy context.

    #1026: входящее сообщение пишет живой человек — самый активный инъекционный
    вектор среди free-text путей (аудит #1025), поэтому оно маркируется как
    недоверенные данные.
    """
    return [
        {
            "role": "system",
            "content": (
                "Напиши короткий вежливый ответ рекрутеру на русском. "
                "Не выдумывай факты, не отправляй сообщение и верни только текст ответа.\n"
                + UNTRUSTED_DATA_INSTRUCTION
            ),
        },
        {
            "role": "user",
            "content": (
                f"Вакансия: {context.vacancy_title}\nКомпания: {context.employer or 'не указана'}\n"
                "Входящее сообщение рекрутера:\n"
                + wrap_untrusted("сообщение рекрутера", context.inbound_text)
                + "\nСоставь уместный ответ именно на это сообщение."
            ),
        },
    ]


def suggest(contexts: list[ReplyContext], llm_client) -> str:
    """Generate one suggestion. Caller persists it as a draft."""
    context = validate_context(contexts)
    response = llm_client.chat(build_prompt(context), temperature=0.4)
    text = (getattr(response, "content", None) or "").strip()
    if not text:
        raise RuntimeError("LLM returned an empty reply suggestion")
    return text
