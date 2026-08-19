"""Read-only detection of portfolio/project evidence requests in vacancies.

The first stage is deliberately deterministic.  It requires both an artefact
term (GitHub, demo, portfolio, etc.) and language asking for or encouraging a
link/example.  This keeps incidental mentions such as ``GitHub Actions`` and
company GitHub pages out of the signal.  An optional LLM stage may refine an
ambiguous keyword match, but can never invent a requirement when the model is
unavailable or uncertain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

PortfolioEvidenceLevel = Literal["required", "preferred", "none"]


@dataclass(frozen=True)
class PortfolioEvidenceRequirement:
    """Structured, explainable portfolio evidence signal."""

    level: PortfolioEvidenceLevel
    evidence: tuple[str, ...] = ()
    rationale: str = ""
    confidence: float = 0.0
    source: Literal["keyword", "llm", "keyword+llm", "none"] = "none"


# Keep terms specific enough that a technology mention alone is not a hit.
_ARTEFACT = re.compile(
    r"(?i)(?:github|gitlab|репозитор(?:ий|ии)|portfolio|портфолио|"
    r"project(?:s)?|проект(?:ы|ов)?|demo(?:s)?|демо|case\s*stud(?:y|ies)|"
    r"кейс(?:ы|ов)?|bot(?:s)?|бот(?:ы|ов)?|deployed\s+service|"
    r"рабоч(?:ий|ие)\s+(?:сервис|сервисы))"
)
_REQUEST = re.compile(
    r"(?i)(?:attach|include|provide|send|share|add|link|links?|"
    r"приложить|приложите|прикрепить|прикрепите|указать|укажите|"
    r"предоставить|пришлите|ссылк(?:а|у|и|и на)|покажите|"
    r"расскажите\s+о|примеры?\s+(?:работы|проектов))"
)
_REQUIRED = re.compile(
    r"(?i)(?:обязательн|must|require[ds]?|need(?:ed)?|please\s+attach|"
    r"пришлите|приложите|прикрепите|предоставьте|укажите)"
)
_PREFERRED = re.compile(
    r"(?i)(?:желательно|будет\s+плюсом|плюсом|приветствуются?|"
    r"preferred|nice\s+to\s+have|would\s+be\s+great|можно\s+приложить|"
    r"если\s+есть)"
)
_COMPANY_LINK = re.compile(
    r"(?i)(?:company|компани[ия]|работодател[яь]).{0,30}(?:github|gitlab)|(?:github|gitlab).{0,30}(?:company|компани[ия]|работодател[яь])"
)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]|$)")


def _evidence_sentences(text: str, spans: list[tuple[int, int]]) -> tuple[str, ...]:
    result: list[str] = []
    for sentence in _SENTENCE.finditer(text):
        if any(
            sentence.start() <= start < sentence.end() or sentence.start() < end <= sentence.end()
            for start, end in spans
        ):
            value = " ".join(sentence.group(0).split()).strip()
            if value and value not in result:
                result.append(value)
    return tuple(result)


def detect_portfolio_evidence(text: str | None) -> PortfolioEvidenceRequirement:
    """Detect explicit requests for links/examples from vacancy text.

    A match is returned only when an artefact and request phrase occur in the
    same sentence.  The source sentence is retained for downstream review.
    """
    if not text or not text.strip():
        return PortfolioEvidenceRequirement("none")
    spans: list[tuple[int, int]] = []
    levels: list[str] = []
    for sentence in _SENTENCE.finditer(text):
        value = sentence.group(0)
        artefacts = list(_ARTEFACT.finditer(value))
        requests = list(_REQUEST.finditer(value))
        if not artefacts or not requests or _COMPANY_LINK.search(value):
            continue
        spans.append((sentence.start(), sentence.end()))
        levels.append("required" if _REQUIRED.search(value) else "preferred")
    if not spans:
        return PortfolioEvidenceRequirement("none")
    level: PortfolioEvidenceLevel = "required" if "required" in levels else "preferred"
    evidence = _evidence_sentences(text, spans)
    return PortfolioEvidenceRequirement(
        level,
        evidence=evidence,
        rationale="explicit evidence request detected by keyword rules",
        confidence=0.95 if level == "required" else 0.82,
        source="keyword",
    )


class PortfolioEvidenceLLM(Protocol):
    def classify(self, text: str) -> str | None: ...


def classify_portfolio_evidence(
    text: str | None, llm: PortfolioEvidenceLLM | None = None
) -> PortfolioEvidenceRequirement:
    """Run keyword detection, optionally refining it with a strict LLM result.

    LLM output is accepted only as ``required``/``preferred`` JSON with a
    confidence of at least 0.7.  Failures retain keyword evidence; an LLM
    cannot turn an incidental mention into a requirement.
    """
    keyword = detect_portfolio_evidence(text)
    if llm is None or not text or keyword.level == "none":
        return keyword
    try:
        raw = llm.classify(text)
        data = json.loads(raw or "")
        level = data.get("level")
        confidence = float(data.get("confidence", 0))
        if level not in ("required", "preferred", "none") or confidence < 0.7:
            return keyword
        if level == "none":
            return keyword
        return PortfolioEvidenceRequirement(
            level,
            evidence=keyword.evidence,
            rationale=str(data.get("rationale") or keyword.rationale)[:240],
            confidence=min(confidence, 1.0),
            source="keyword+llm",
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return keyword
