"""Read-only inspection helpers for the authenticated negotiations page."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape

logger = logging.getLogger("hhru_bot.negotiations_probe")

_STATE_RE = re.compile(
    r'<template[^>]*id=["\']HH-Lux-InitialState["\'][^>]*>(.*?)</template>', re.DOTALL
)


@dataclass(frozen=True)
class TopicRef:
    topic_id: str
    chat_id: str
    vacancy_id: str | None = None


def parse_initial_state(html: str) -> dict:
    """Read the SSR state without executing scripts or interacting with the page."""
    match = _STATE_RE.search(html)
    if not match:
        raise ValueError("SSR state template HH-Lux-InitialState not found")
    return json.loads(unescape(match.group(1)))


def topic_refs(html: str) -> list[TopicRef]:
    """Return the topic/chat mapping rendered in the negotiations SSR state.

    A topic entry missing ``id``/``chatId``/``vacancyId`` is dropped (fetch_responses
    can't attach it to a card without a vacancy_id to key on); dropped entries are
    logged so a silently shrinking mapping is diagnosable, matching the warning-log
    contract of the SSR-recovery except-path in responses.py.
    """
    topics = parse_initial_state(html).get("applicantNegotiations", {}).get("topicList", [])
    refs: list[TopicRef] = []
    for topic in topics:
        if topic.get("id") is None or topic.get("chatId") is None or topic.get("vacancyId") is None:
            logger.debug("SSR topic entry missing id/chatId/vacancyId, dropped: %r", topic)
            continue
        refs.append(TopicRef(str(topic["id"]), str(topic["chatId"]), str(topic["vacancyId"])))
    return refs


def chat_url(chat_id: str, chatik_origin: str = "https://chatik.hh.ru") -> str:
    """Build the route used by hh.ru's ``open_chat`` button."""
    return f"{chatik_origin.rstrip('/')}/chat/{chat_id}"
