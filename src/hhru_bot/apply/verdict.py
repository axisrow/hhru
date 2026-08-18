"""Typed, fail-closed model for reading negotiations (#213).

The browser reader is intentionally kept outside this module.  This module
contains only the contract and the decision table, so changes to SSR/DOM
parsing cannot silently change the uncertainty policy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class PageSource(StrEnum):
    SSR = "SSR"
    DOM = "DOM"


class ResumeAttribution(StrEnum):
    MATCHED = "matched"
    FOREIGN = "foreign"
    INCOMPARABLE = "incomparable"
    OTHER_OWN = "other_own"


class Completeness(StrEnum):
    LAST_CONFIRMED = "last_confirmed"
    UNRENDERED = "unrendered"


@dataclass(frozen=True)
class Partial:
    reason: str


@dataclass(frozen=True)
class TopicRead:
    vacancy_id: str
    resume_attribution: ResumeAttribution = ResumeAttribution.MATCHED
    detail: str = ""


@dataclass(frozen=True)
class PageRead:
    source: PageSource
    topics: tuple[TopicRead, ...]
    completeness: Completeness | Partial


FOUND = "found"
NOT_FOUND = "not_found"
INDETERMINATE = "indeterminate"


def compose(attempts: Iterable[PageRead], vacancy_id: str) -> str:
    """Compose a verdict from reads using one fail-closed decision table."""
    reads = tuple(attempts)
    if any(
        topic.vacancy_id == vacancy_id and topic.resume_attribution == ResumeAttribution.MATCHED
        for read in reads
        for topic in read.topics
    ):
        return FOUND
    if any(
        topic.vacancy_id == vacancy_id
        and topic.resume_attribution
        in {
            ResumeAttribution.INCOMPARABLE,
            ResumeAttribution.OTHER_OWN,
        }
        for read in reads
        for topic in read.topics
    ):
        return INDETERMINATE
    if any(isinstance(read.completeness, Partial) for read in reads) or any(
        read.completeness == Completeness.UNRENDERED for read in reads
    ):
        return INDETERMINATE
    return (
        NOT_FOUND
        if any(read.completeness == Completeness.LAST_CONFIRMED for read in reads)
        else INDETERMINATE
    )
