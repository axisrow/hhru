"""Read-only inspection helpers for the authenticated negotiations page."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape

_STATE_RE = re.compile(
    r'<template[^>]*id=["\']HH-Lux-InitialState["\'][^>]*>(.*?)</template>', re.DOTALL
)


@dataclass(frozen=True)
class TopicRef:
    topic_id: str
    chat_id: str


def parse_initial_state(html: str) -> dict:
    """Read the SSR state without executing scripts or interacting with the page."""
    match = _STATE_RE.search(html)
    if not match:
        raise ValueError("SSR state template HH-Lux-InitialState not found")
    return json.loads(unescape(match.group(1)))


def topic_refs(html: str) -> list[TopicRef]:
    """Return the topic/chat mapping rendered in the negotiations SSR state."""
    topics = parse_initial_state(html).get("applicantNegotiations", {}).get("topicList", [])
    return [
        TopicRef(str(topic["id"]), str(topic["chatId"]))
        for topic in topics
        if topic.get("id") is not None and topic.get("chatId") is not None
    ]


def chat_url(chat_id: str, chatik_origin: str = "https://chatik.hh.ru") -> str:
    """Build the route used by hh.ru's ``open_chat`` button."""
    return f"{chatik_origin.rstrip('/')}/chat/{chat_id}"
