"""Read-only helpers for employer messages in negotiations chats.

The chat DOM is intentionally kept out of the domain logic: selectors for the
authenticated ``/chat`` page still need confirmation against a live account.
Once a message's text has been read, link detection is deterministic and does
not perform any navigation (in particular, it never follows the external URL).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

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
