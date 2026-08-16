"""Read-only helpers for employer messages in negotiations chats.

The chat DOM is intentionally kept out of the domain logic: selectors for the
authenticated ``/chat`` page still need confirmation against a live account.
Once a message's text has been read, link detection is deterministic and does
not perform any navigation (in particular, it never follows the external URL).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Page

from .browser import goto_hh
from .negotiations_probe import chat_url
from .selector_groups.negotiations import (
    CHAT_MESSAGE_MY_MARKER,
    CHAT_MESSAGE_OTHER_MARKER,
    CHAT_MESSAGE_TEXT,
)

# A URL is deliberately restricted to HTTP(S).  This avoids treating email
# addresses, javascript: values, and arbitrary punctuation as test links.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>\"'»"
_HH_DOMAINS = ("hh.ru", "hhcdn.ru")


@dataclass(frozen=True)
class ChatMessage:
    """The small, browser-independent part of the latest chat message."""

    author: str | None
    inbound_marker: str | None


@dataclass(frozen=True)
class ReplyDecision:
    """Result of the fail-closed reply decision."""

    should_reply: bool
    reason: str


def needs_reply(chat: ChatMessage | None) -> ReplyDecision:
    """Decide whether the latest message permits a reply.

    ``author`` is deliberately normalized by the DOM reader to ``employer`` or
    ``me``.  A missing message, author, or marker is never treated as an
    employer message: sending on incomplete DOM data would create a duplicate.
    """
    if chat is None:
        return ReplyDecision(False, "empty_chat")
    if not chat.inbound_marker:
        return ReplyDecision(False, "inbound_marker_unknown")
    if chat.author == "employer":
        return ReplyDecision(True, "last_message_from_employer")
    if chat.author == "me":
        return ReplyDecision(False, "last_message_from_us")
    return ReplyDecision(False, "author_unknown")


def _message_id(data_qa: str | None) -> str | None:
    if not data_qa or not data_qa.startswith("chatik-chat-message-"):
        return None
    value = data_qa[len("chatik-chat-message-") :]
    if not value.endswith("-text"):
        return None
    marker = value[: -len("-text")]
    return marker or None


def read_last_message(page: Page, chat_id: str) -> ChatMessage | None:
    """Read the latest message from the confirmed chat route, without writes."""
    goto_hh(page, chat_url(chat_id))
    messages = page.locator(CHAT_MESSAGE_TEXT)
    if not messages.count():
        return None
    message = messages.nth(messages.count() - 1)
    marker = _message_id(message.get_attribute("data-qa"))
    author = message.evaluate(
        """(el, ownMarker, otherMarker) => {
            for (let node = el; node; node = node.parentElement) {
                const classes = String(node.className).split(/\\s+/);
                if (classes.includes(ownMarker)) return 'me';
                if (classes.includes(otherMarker)) return 'employer';
            }
            return null;
        }""",
        CHAT_MESSAGE_MY_MARKER,
        CHAT_MESSAGE_OTHER_MARKER,
    )
    return ChatMessage(author, marker)


def read_chat(
    page: Page, topic: str, topic_to_chat_id: Mapping[str, str]
) -> ChatMessage | None:
    """Resolve a topic from the #107 SSR mapping and read its latest message."""
    chat_id = topic_to_chat_id.get(str(topic))
    if not chat_id:
        return None
    return read_last_message(page, str(chat_id))


def _is_hh_domain(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in _HH_DOMAINS)


def extract_external_test_link(message_text: str) -> str | None:
    """Return the first non-hh.ru HTTP(S) URL in an employer message.

    ``hh.ru`` and ``hhcdn.ru`` (including their subdomains) are internal links
    and are ignored.  If a message contains multiple links, the first external
    one is returned.  The function only parses text; it never makes a request.
    """
    for match in _URL_RE.finditer(message_text):
        url = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme.lower() in {"http", "https"} and not _is_hh_domain(parsed.hostname):
            return url
    return None


def read_employer_messages(page: Page, chat_id: str) -> list[str]:
    """Read all employer messages through the confirmed chat route, newest first.

    This performs only GET navigation and DOM reads. Messages are inspected in
    reverse DOM order; ``message_my`` is skipped, so the caller sees every
    employer message, not just the latest one — a test-assignment link can sit
    in an earlier message even if the employer's most recent message is a
    URL-free follow-up.
    """
    goto_hh(page, chat_url(chat_id))
    messages = page.locator(CHAT_MESSAGE_TEXT)
    texts: list[str] = []
    for index in range(messages.count() - 1, -1, -1):
        message = messages.nth(index)
        is_own = message.evaluate(
            """(el, marker) => {
                for (let node = el; node; node = node.parentElement) {
                    if (String(node.className).split(/\\s+/).includes(marker)) return true;
                }
                return false;
            }""",
            CHAT_MESSAGE_MY_MARKER,
        )
        if not is_own:
            text = message.inner_text().strip()
            if text:
                texts.append(text)
    return texts
