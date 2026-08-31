"""Identity-bound resume state extracted from hh.ru bootstrap markup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeProfessionalRole:
    role_id: str
    label: str | None = None


@dataclass
class ResumeState:
    status: str | None = None
    is_searchable: bool | None = None
    can_publish_or_update: bool | None = None
    next_incomplete_screen_id: str | None = None
    professional_roles: tuple[ResumeProfessionalRole, ...] = ()
    title: str | None = None


def is_published(state: ResumeState) -> bool:
    """Return the positive live signal for a published hh.ru resume."""
    return state.is_searchable is True or state.status == "finished"


def _walk_json(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def parse_resume_state(markup: str, resume_id: str) -> ResumeState:
    """Extract state only from the structured record for ``resume_id``."""
    if not resume_id:
        raise ValueError("resume_id is required to parse resume state safely")

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", markup):
        try:
            candidate, _ = decoder.raw_decode(markup[match.start() :])
        except json.JSONDecodeError:
            continue
        for record in _walk_json(candidate):
            if not isinstance(record, dict):
                continue
            attributes = record.get("_attributes")
            identity_record = attributes if isinstance(attributes, dict) else record
            identifiers = {str(identity_record.get(key, "")) for key in ("id", "hash", "resumeId")}
            if resume_id not in identifiers:
                continue
            # hh.ru keeps the wizard's ``scheme`` next to the resume record.
            # Attach it only after finding the requested identity in this same
            # JSON document; never merge fields from records for other resumes.
            scheme = candidate.get("scheme") if isinstance(candidate, dict) else None
            return _state_from_mapping(identity_record, scheme, details=record)
    return ResumeState()


def _state_from_mapping(
    record: dict,
    scheme: dict | None = None,
    *,
    details: dict | None = None,
) -> ResumeState:
    next_incomplete = record.get("nextIncompleteScreenId")
    if next_incomplete is None and isinstance(scheme, dict):
        next_incomplete = scheme.get("nextIncompleteScreenId")
    roles = _parse_professional_roles(details or record)
    title = _parse_resume_title(details or record)
    return ResumeState(
        status=record.get("status"),
        is_searchable=record.get("isSearchable"),
        can_publish_or_update=record.get("canPublishOrUpdate"),
        next_incomplete_screen_id=next_incomplete,
        professional_roles=roles,
        title=title,
    )


def _parse_resume_title(record: dict) -> str | None:
    """Read the draft title from the identity-bound bootstrap record."""
    for key in ("title", "desiredPosition"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    position = record.get("position")
    if isinstance(position, dict):
        value = position.get("title")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_professional_roles(record: dict) -> tuple[ResumeProfessionalRole, ...]:
    raw_roles = record.get("professionalRole")
    if not isinstance(raw_roles, list):
        return ()
    parsed: list[ResumeProfessionalRole] = []
    for item in raw_roles:
        if not isinstance(item, dict):
            continue
        role_id = item.get("id", item.get("string"))
        if role_id is None:
            continue
        label = item.get("text")
        parsed.append(
            ResumeProfessionalRole(
                role_id=str(role_id),
                label=label.strip() if isinstance(label, str) and label.strip() else None,
            )
        )
    return tuple(parsed)
