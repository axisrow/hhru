"""Shared privacy validation for applicant-controlled competitor skills."""

from __future__ import annotations

import re

_CONTACT_RE = re.compile(
    r"(?:[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}|https?://\S+|www\.\S+|@[A-Za-z0-9_.-]+|(?:\+?\d[\d\s().-]{8,}\d))",
    re.I,
)
_DOMAIN_RE = re.compile(r"^(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?P<path>/\S*)?$", re.I)
_CONTACT_TLDS = {"com", "ru", "org", "me", "co", "рф"}


def is_contact_skill(value: str) -> bool:
    text = value.strip()
    if not text or _CONTACT_RE.search(text):
        return True
    match = _DOMAIN_RE.fullmatch(text)
    if not match:
        return False
    if match.group("path"):
        return True
    suffix = match.group("host").rsplit(".", 1)[-1].casefold()
    return suffix in _CONTACT_TLDS or text == text.casefold()


def sanitize_skill_name(value: str) -> str | None:
    text = value.strip()
    return None if not text or is_contact_skill(text) else text
