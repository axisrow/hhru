"""Read-only helpers for employer messages in negotiations chats.

The chat DOM is intentionally kept out of the domain logic: selectors for the
authenticated ``/chat`` page still need confirmation against a live account.
Once a message's text has been read, link detection is deterministic and does
not perform any navigation (in particular, it never follows the external URL).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.sync_api import Page

from .browser import goto_hh
from .selector_groups.negotiations import CHAT_MESSAGE_MY_MARKER, CHAT_MESSAGE_TEXT

# A URL is deliberately restricted to HTTP(S).  This avoids treating email
# addresses, javascript: values, and arbitrary punctuation as test links.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>\"'»"
_HH_DOMAINS = ("hh.ru", "hhcdn.ru")


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
    goto_hh(page, f"https://chatik.hh.ru/chat/{chat_id}")
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
