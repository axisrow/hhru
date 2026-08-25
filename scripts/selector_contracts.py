#!/usr/bin/env python3
"""Build and verify the HH.ru selector provenance catalog.

The catalog is deliberately repository-local.  CI never executes code from the
reference projects: refresh only reads source files from three checked-out,
public repositories and records selector literals plus their commit SHAs.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "selectors" / "reference-map.yaml"
MATRIX_PATH = ROOT / "selectors" / "reference-matrix.md"
EVIDENCE_PATH = ROOT / "selectors" / "live-evidence.json"
BASELINE_PATH = ROOT / "selectors" / "baseline-599.json"
GENERATED_PATH = ROOT / "src" / "hhru_bot" / "selector_groups" / "_generated.py"
SOURCE_ROOT = ROOT / "src" / "hhru_bot"

REFERENCE_CONFIG = {
    "steev": {
        "repository": "Steev193/hh-ru-apply",
        "directory": "hh-ru-apply",
        "license": "MIT",
    },
    "tgeruzov": {
        "repository": "tgeruzov/hh-auto-responder",
        "directory": "hh-auto-responder",
        "license": "MIT",
    },
    "yamakayama": {
        "repository": "YAMAKAYAMACO/hh-autoresponder",
        "directory": "hh-autoresponder",
        "license": "MIT",
    },
}

# Narrow, reviewed exceptions where exact 2-of-3 or a captured DOM state is
# impossible by design.  These do not authorize a write click.
SPECIAL_POLICIES: dict[str, tuple[str, str]] = {
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.0.1": (
        "safety_guard",
        "fail-closed captcha detector; absence never authorizes a write",
    ),
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.1.1": (
        "safety_guard",
        "fail-closed captcha detector; absence never authorizes a write",
    ),
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.2.1": (
        "safety_guard",
        "fail-closed captcha detector; absence never authorizes a write",
    ),
    "competitor_resume.PAGINATION_BLOCK": (
        "structural_read_fallback",
        "read-only public-resume pagination fallback; cannot trigger a mutation",
    ),
    "competitor_resume.PAGINATION_NEXT": (
        "structural_read_fallback",
        "read-only public-resume pagination fallback; cannot trigger a mutation",
    ),
    "competitor_resume.PAGINATION_PAGE": (
        "structural_read_fallback",
        "read-only public-resume pagination fallback; cannot trigger a mutation",
    ),
    "competitor_resume.SEARCH_EMPTY": (
        "structural_read_fallback",
        "read-only empty-state union; callers still require confirmed page state",
    ),
    "negotiations.LEGACY_NEGOTIATION_EMPLOYER": (
        "structural_read_fallback",
        "read-only compatibility with saved legacy markup",
    ),
    "negotiations.LEGACY_NEGOTIATION_VACANCY_LINK": (
        "structural_read_fallback",
        "read-only compatibility with saved legacy markup",
    ),
    "professional_roles.FILTER_TRIGGER": (
        "workflow_live",
        "successful cached live catalog proves the read-only discovery workflow",
    ),
    "professional_roles.TREE_INPUT": (
        "workflow_live",
        "successful cached live catalog proves the read-only discovery workflow",
    ),
    "professional_roles.TREE_INPUT_ANY": (
        "workflow_live",
        "successful cached live catalog proves the read-only discovery workflow",
    ),
    "resume_page.RESUME_BUMP_DISABLED_HINT": (
        "documented_live",
        "confirmed by the bump workflow live check; optional read-only hint",
    ),
}

# Selectors that were previously assembled dynamically or lived outside the
# module-level declarations discovered during bootstrap.  Keeping them here is
# intentional: it makes the migration reproducible without teaching the AST
# scanner how to execute project code.
EXTRA_CONTRACTS: dict[str, dict[str, str]] = {
    "apply_form.APPLY_RESUME_OPTION": {
        "value": "[data-qa='magritte-select-option-{resume_id}']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/selector_groups/apply_form.py:1",
    },
    "negotiations.CHAT_MESSAGE_ROOT": {
        "value": "[data-qa^='chatik-chat-message-']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/selector_groups/negotiations.py:1",
        "decision": "documented_live",
    },
    "negotiations.CHAT_AUTHOR_HINT": {
        "value": "[data-qa*='author'], [class*='author'], [aria-label], [title]",
        "criticality": "read",
        "declared_at": "src/hhru_bot/negotiations_chat.py:1",
        "decision": "documented_live",
    },
    "resume_list.RESUME_LIST_CARD_LINK_PREFIX": {
        "value": "[data-qa^='resume-card-link-']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/selector_groups/resume_list.py:1",
    },
    "create_resume.TREE_ITEM_TEXT": {
        "value": "[data-qa*='tree-selector-item-text-']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/create_resume.py:1",
        "decision": "workflow_live",
    },
    "professional_roles.TREE_CATEGORY_INPUT": {
        "value": "input[data-qa*='tree-selector-input-category-']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/professional_roles.py:1",
        "decision": "workflow_live",
    },
    "professional_roles.TREE_CHEVRON": {
        "value": "[data-qa~='tree-selector-chevron-category-{category_id}']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/professional_roles.py:1",
        "decision": "workflow_live",
    },
    "resume_position.CURRENCY_TEMPLATE": {
        "value": "[data-qa='resume-currency-input-{code}']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_TITLE": {
        "value": "[data-qa='resume-block-title-position']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_SALARY": {
        "value": "[data-qa='resume-block-salary']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_EMPLOYMENT": {
        "value": "[data-qa='resume-position-field-employmentForms']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_WORK_FORMAT": {
        "value": "[data-qa='resume-position-field-workFormats']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_TRAVEL": {
        "value": "[data-qa='resume-position-field-travelTime']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_position.DISPLAY_BUSINESS_TRIPS": {
        "value": "[data-qa='resume-position-field-businessTripReadiness']",
        "criticality": "read",
        "declared_at": "src/hhru_bot/resume_position.py:1",
    },
    "resume_sections.ATTESTATION_SELECTOR.0": {
        "value": "[data-qa='profile-education-attestation-name']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/resume_sections.py:1",
    },
    "resume_sections.ATTESTATION_SELECTOR.1": {
        "value": "[data-qa='profile-education-attestation-organization']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/resume_sections.py:1",
    },
    "resume_sections.ATTESTATION_SELECTOR.2": {
        "value": "[data-qa='profile-education-attestation-specialty']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/resume_sections.py:1",
    },
    "resume_sections.ATTESTATION_SELECTOR.3": {
        "value": "[data-qa='profile-education-year-input']",
        "criticality": "write",
        "declared_at": "src/hhru_bot/resume_sections.py:1",
    },
}

# One-time semantic joins for selectors that implement the same UI role but
# are not textually equal (for example, an upstream selector adds a tag name or
# keeps an older HH.ru variant).  Exact matches are bound automatically.
REFERENCE_BINDING_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "apply.success.APPLY_SUCCESS_MARKER": {
        "tgeruzov": ("script.js::isResponseConfirmed#0::1",),
        "yamakayama": ("app/parsers/hh_playwright.py::module.HHPlaywright.apply_to_vacancy#6::1",),
    },
    "apply_form.APPLY_COVER_LETTER_TEXTAREA": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright._fill_response_form#0::0",
        ),
    },
    "apply_form.APPLY_RESUME_OPTION": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright._fill_response_form.first_resume#0::0",
        ),
    },
    "apply_form.APPLY_RESUME_SELECT": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright._fill_response_form.resume_select#0::0",
        ),
    },
    "negotiations.CHAT_MESSAGE_INPUT": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright.send_thanks_via_clicks.input_selectors#0::0",
        ),
    },
    "negotiations.CHAT_MESSAGE_SEND": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright.send_thanks_via_clicks.send_selectors#0::0",
        ),
    },
    "negotiations.NEGOTIATION_STATUS": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright._parse_negotiation_item.status_el#0::0",
        ),
    },
    "negotiations.NEGOTIATION_VACANCY_LINK": {
        "yamakayama": (
            "app/parsers/hh_playwright.py::module.HHPlaywright._parse_negotiation_item.title_el#0::0",
        ),
    },
    "search_page.VACANCY_CARD": {
        "tgeruzov": ("script.js::module.SELECTORS.vacancyCard#0::1",),
    },
    "search_page.VACANCY_CARD_TITLE_LINK": {
        "tgeruzov": ("script.js::module.SELECTORS.vacancyLink#0::1",),
    },
    "selectors.LOGIN_CODE_INPUT": {
        "yamakayama": ("app/parsers/hh_login.py::module.SEL_PIN#0::0",),
    },
    "selectors.LOGIN_EMAIL_INPUT": {
        "yamakayama": ("app/parsers/hh_login.py::module.SEL_LOGIN#0::0",),
    },
}

SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx"}
SELECTOR_MARKERS = ("[data-qa", "[data-qa-popup-error-code")
_QUOTED = re.compile(r"(?P<quote>['\"`])(?P<body>(?:\\.|(?!\1).)*)(?P=quote)", re.DOTALL)
_DATA_QA_ATTR = re.compile(r"\bdata-qa\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_LONG_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])(?:[0-9]{7,}|[a-f0-9]{20,})(?![A-Za-z0-9])", re.I)


@dataclass(frozen=True)
class SourceSelector:
    value: str
    normalized: str
    file: str
    line: int
    key: str


def normalize_selector(value: str) -> str:
    value = html.unescape(value.strip())
    value = re.sub(r"\s+", " ", value)
    value = value.replace('data-qa="', "data-qa='").replace('"]', "']")
    value = value.replace('data-qa-popup-error-code="', "data-qa-popup-error-code='")
    return value


def _contains_selector(value: str) -> bool:
    return any(marker in value for marker in SELECTOR_MARKERS)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _split_selector_list(value: str) -> list[str]:
    """Split a CSS selector list without splitting commas inside brackets."""
    parts: list[str] = []
    start = 0
    bracket = paren = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "," and not bracket and not paren:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if _contains_selector(part)]


def _selector_candidates(value: str) -> list[str]:
    candidates = [value]
    candidates.extend(_split_selector_list(value))
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        normalized = normalize_selector(candidate)
        if normalized not in seen:
            seen.add(normalized)
            result.append(candidate.strip())
    return result


def _python_scope_keys(tree: ast.AST) -> dict[int, str]:
    keys: dict[int, str] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope = "module"
            self.ordinals: defaultdict[str, int] = defaultdict(int)
            self.binding: str | None = None

        def _visit_scope(self, node: ast.AST, name: str) -> None:
            previous = self.scope
            self.scope = f"{previous}.{name}"
            self.generic_visit(node)
            self.scope = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node, node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node, node.name)

        def _visit_binding(self, value: ast.AST | None, name: str) -> None:
            previous = self.binding
            self.binding = name
            if value is not None:
                self.visit(value)
            self.binding = previous

        def visit_Assign(self, node: ast.Assign) -> None:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self._visit_binding(node.value, node.targets[0].id)
                return
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                self._visit_binding(node.value, node.target.id)
                return
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and _contains_selector(node.value):
                scope = f"{self.scope}.{self.binding}" if self.binding else self.scope
                ordinal = self.ordinals[scope]
                self.ordinals[scope] += 1
                keys[id(node)] = f"{scope}#{ordinal}"

    Visitor().visit(tree)
    return keys


def extract_python_selectors(path: Path, base: Path) -> list[SourceSelector]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(text, filename=str(path))
    scope_keys = _python_scope_keys(tree)
    result: list[SourceSelector] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not _contains_selector(node.value):
            continue
        scope = scope_keys.get(id(node), f"module#line-{node.lineno}")
        for component, candidate in enumerate(_selector_candidates(node.value)):
            result.append(
                SourceSelector(
                    value=candidate,
                    normalized=normalize_selector(candidate),
                    file=str(path.relative_to(base)),
                    line=node.lineno,
                    key=f"{path.relative_to(base)}::{scope}::{component}",
                )
            )
    return result


def _javascript_scope(text: str, offset: int) -> str:
    prefix = text[:offset]
    matches = list(
        re.finditer(
            r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|"
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
            prefix,
        )
    )
    if not matches:
        return "module"
    match = matches[-1]
    return match.group(1) or match.group(2) or "module"


def _javascript_literal_scope(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    line_prefix = text[line_start:offset]
    assignment = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*$", line_prefix)
    if assignment:
        return f"module.{assignment.group(1)}"
    property_match = re.search(r"([A-Za-z_$][\w$]*)\s*:\s*$", line_prefix)
    if property_match:
        containers = list(
            re.finditer(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{", text[:offset])
        )
        if containers:
            return f"module.{containers[-1].group(1)}.{property_match.group(1)}"
    return _javascript_scope(text, offset)


def extract_javascript_selectors(path: Path, base: Path) -> list[SourceSelector]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    scope_ordinals: defaultdict[str, int] = defaultdict(int)
    result: list[SourceSelector] = []
    for match in _QUOTED.finditer(text):
        value = match.group("body")
        if not _contains_selector(value):
            continue
        value = value.replace('\\"', '"').replace("\\'", "'").replace("\\`", "`")
        scope = _javascript_literal_scope(text, match.start())
        ordinal = scope_ordinals[scope]
        scope_ordinals[scope] += 1
        for component, candidate in enumerate(_selector_candidates(value)):
            result.append(
                SourceSelector(
                    value=candidate,
                    normalized=normalize_selector(candidate),
                    file=str(path.relative_to(base)),
                    line=_line_number(text, match.start()),
                    key=f"{path.relative_to(base)}::{scope}#{ordinal}::{component}",
                )
            )
    return result


def extract_reference(reference_root: Path) -> list[SourceSelector]:
    result: list[SourceSelector] = []
    for path in sorted(reference_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES or ".git" in path.parts:
            continue
        try:
            if path.suffix.lower() == ".py":
                result.extend(extract_python_selectors(path, reference_root))
            else:
                result.extend(extract_javascript_selectors(path, reference_root))
        except (SyntaxError, UnicodeError):
            continue
    return result


def _git_sha(path: Path) -> str:
    process = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout.strip()


def _literal_strings(value: Any, suffix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        if _contains_selector(value):
            yield suffix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _literal_strings(item, f"{suffix}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _literal_strings(item, f"{suffix}.{index}")


def discover_declared_selectors() -> dict[str, dict[str, Any]]:
    """Discover named, module-level selector declarations before migration."""
    selectors: dict[str, dict[str, Any]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path.name == "_generated.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = str(path.relative_to(SOURCE_ROOT).with_suffix("")).replace("/", ".")
        if module.startswith("selector_groups."):
            module = module.removeprefix("selector_groups.")
        for node in tree.body:
            name: str | None = None
            value_node: ast.AST | None = None
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                value_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                value_node = node.value
            if not name or not name.isupper() or value_node is None:
                continue
            try:
                value = ast.literal_eval(value_node)
            except (ValueError, TypeError):
                continue
            for suffix, selector_value in _literal_strings(value):
                logical_id = f"{module}.{name}{suffix}"
                selectors[logical_id] = {
                    "value": selector_value,
                    "criticality": classify_criticality(logical_id),
                    "declared_at": f"{path.relative_to(ROOT)}:{node.lineno}",
                }
    return selectors


def infer_criticality(logical_id: str) -> str:
    lowered = logical_id.lower()
    write_words = (
        "apply",
        "submit",
        "save",
        "delete",
        "send",
        "publish",
        "duplicate",
        "edit",
        "create",
        "input",
        "cancel",
        "confirm",
        "withdraw",
        "toggle",
        "option",
    )
    return "write" if any(word in lowered for word in write_words) else "read"


def classify_criticality(logical_id: str) -> str:
    """Classify by write-path reachability, not only by the clicked element.

    A read-looking title or status selector is WRITE-critical when a mutation
    uses it for identity, scoping, or post-save verification.
    """
    if SPECIAL_POLICIES.get(logical_id, (None,))[0] == "structural_read_fallback":
        return "read"
    if logical_id.startswith("professional_roles."):
        return "write" if logical_id == "professional_roles.TREE_LABEL" else "read"
    if logical_id in {
        "vacancy_page.VACANCY_DESCRIPTION",
        "vacancy_page.VACANCY_EXPERIENCE",
        "vacancy_page.VACANCY_VIEW_EMPLOYMENT_MODE",
        "vacancy_page.VACANCY_VIEW_LOCATION",
        "vacancy_page.VACANCY_VIEW_RAW_ADDRESS",
    }:
        return "read"
    write_prefixes = (
        "apply.",
        "apply_form.",
        "create_resume.",
        "negotiations.",
        "resume_education.",
        "resume_experience.",
        "resume_list.",
        "resume_page.",
        "resume_position.",
        "resume_sections.",
        "selectors.LOGIN",
        "vacancy_page.",
    )
    if logical_id.startswith(write_prefixes):
        return "write"
    return infer_criticality(logical_id)


def documented_evidence(declared_at: str) -> dict[str, str] | None:
    """Return a source-backed live-observation note from the declaration context.

    This is intentionally conservative: an explicit negative marker nearest the
    declaration wins over a broad module-level claim.
    """
    raw_path, raw_line = declared_at.rsplit(":", 1)
    path = ROOT / raw_path
    line = int(raw_line)
    lines = path.read_text(encoding="utf-8").splitlines()
    nearby = lines[max(0, line - 24) : line]
    closest = "\n".join(nearby[-10:]).lower()
    negative = (
        "не подтвержден",
        "не провер",
        "recheck",
        "перепровер",
        "не работает",
        "устарел",
        "candidate",
        "кандидат",
    )
    if any(marker in closest for marker in negative):
        return None
    context = nearby + lines[: min(24, len(lines))]
    positive = (
        "confirmed",
        "подтвержден",
        "сверен",
        "live dom",
        "live read-only",
        "live check",
        "authenticated live",
        "живым dom",
        "живой dom",
        "devtools",
        "f12",
    )
    for candidate in reversed(context):
        lowered = candidate.lower()
        if any(marker in lowered for marker in positive) and not any(
            marker in lowered for marker in negative
        ):
            note = re.sub(r"^\s*[#'\"]+\s*", "", candidate).strip()
            return {"source": declared_at, "note": note[:240]}
    return None


def _screen_for_dump(path: Path) -> str:
    name = path.name.lower()
    if "professional_role" in name:
        return "professional_role"
    if "delete" in name:
        return "resume_delete"
    if "resume-edit" in name or "position" in name:
        return "resume_edit"
    if "publish" in name or "resumes_list" in name:
        return "resume_list"
    if "apply" in name or "probe" in name:
        return "apply"
    return "other"


def _sanitize_qa(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value).strip())
    return _LONG_IDENTIFIER.sub("{id}", value)


def build_live_evidence(live_root: Path) -> dict[str, Any]:
    tokens: dict[str, dict[str, Any]] = {}
    source_hashes: list[str] = []
    for path in sorted(live_root.rglob("*.htm*")):
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        source_hashes.append(digest)
        text = content.decode("utf-8", errors="ignore")
        counts: defaultdict[str, int] = defaultdict(int)
        for match in _DATA_QA_ATTR.finditer(text):
            counts[_sanitize_qa(match.group(2))] += 1
        screen = _screen_for_dump(path)
        for token, count in counts.items():
            entry = tokens.setdefault(
                token,
                {"documents": 0, "occurrences": 0, "screens": set(), "source_hashes": []},
            )
            entry["documents"] += 1
            entry["occurrences"] += count
            entry["screens"].add(screen)
            if len(entry["source_hashes"]) < 5:
                entry["source_hashes"].append(digest)
    serializable = {
        token: {
            "documents": entry["documents"],
            "occurrences": entry["occurrences"],
            "screens": sorted(entry["screens"]),
            "source_hashes": entry["source_hashes"],
        }
        for token, entry in sorted(tokens.items())
    }
    return {
        "version": 1,
        "source_documents": len(source_hashes),
        "source_set_sha256": hashlib.sha256("\n".join(sorted(source_hashes)).encode()).hexdigest(),
        "selectors": serializable,
    }


def _selector_live_matches(selector: str, evidence: dict[str, Any]) -> list[str]:
    tokens = evidence.get("selectors", {})
    patterns = re.findall(r"\[data-qa(?P<op>[\^*$~|]?)=['\"](?P<value>[^'\"]+)['\"]\]", selector)
    if not patterns:
        return []
    matched: set[str] = set()
    for operator, raw_value in patterns:
        raw_value = _sanitize_qa(raw_value)
        template = re.escape(raw_value)
        template = re.sub(r"\\\{[^{}]+\\\}", r".+", template)
        regex = re.compile(f"^{template}$")
        for token in tokens:
            ok = False
            if "{" in raw_value and "}" in raw_value:
                ok = bool(regex.match(token))
            elif operator == "^":
                ok = token.startswith(raw_value)
            elif operator == "$":
                ok = token.endswith(raw_value)
            elif operator == "*":
                ok = raw_value in token
            elif operator == "~":
                ok = raw_value in token.split()
            else:
                ok = token == raw_value
            if ok:
                matched.add(token)
    return sorted(matched)


def _source_dict(item: SourceSelector) -> dict[str, Any]:
    return {"file": item.file, "line": item.line, "key": item.key, "value": item.value}


def _binding_definitions() -> dict[str, dict[str, list[str]]]:
    return {
        logical_id: {reference: list(keys) for reference, keys in references.items()}
        for logical_id, references in REFERENCE_BINDING_KEYS.items()
    }


def _reference_fingerprint(items: list[SourceSelector]) -> str:
    payload = "\n".join(sorted({f"{item.key}\0{item.normalized}" for item in items}))
    return hashlib.sha256(payload.encode()).hexdigest()


def _reference_indexes(
    reference_root: Path,
) -> tuple[dict[str, Any], dict[str, list[SourceSelector]]]:
    metadata: dict[str, Any] = {}
    indexes: dict[str, list[SourceSelector]] = {}
    for name, config in REFERENCE_CONFIG.items():
        path = reference_root / config["directory"]
        if not path.is_dir():
            raise SystemExit(f"reference checkout missing: {path}")
        indexes[name] = extract_reference(path)
        metadata[name] = {
            "repository": config["repository"],
            "license": config["license"],
            "commit": _git_sha(path),
            "selector_fingerprint": _reference_fingerprint(indexes[name]),
        }
    return metadata, indexes


def _matching_sources(value: str, items: list[SourceSelector]) -> list[dict[str, Any]]:
    normalized = normalize_selector(value)
    matches = [_source_dict(item) for item in items if item.normalized == normalized]
    return matches[:8]


def _refresh_bindings(catalog: dict[str, Any], indexes: dict[str, list[SourceSelector]]) -> None:
    for logical_id, row in catalog["selectors"].items():
        requested: defaultdict[str, set[str]] = defaultdict(set)
        for field in ("bindings", "sources"):
            for reference, entries in row.get(field, {}).items():
                requested[reference].update(entry.get("key", "") for entry in entries)
        for gap in row.get("binding_gaps", []):
            reference, separator, key = gap.partition(":")
            if separator and key:
                requested[reference].add(key)
        for reference, keys in REFERENCE_BINDING_KEYS.get(logical_id, {}).items():
            requested[reference].update(keys)
        for reference, items in indexes.items():
            requested[reference].update(
                item.key for item in items if item.normalized == normalize_selector(row["value"])
            )

        bindings: dict[str, list[dict[str, Any]]] = {}
        gaps: list[str] = []
        for reference, keys in requested.items():
            current_by_key = {item.key: item for item in indexes.get(reference, [])}
            resolved = [current_by_key[key] for key in sorted(keys) if key in current_by_key]
            if resolved:
                bindings[reference] = [_source_dict(item) for item in resolved[:12]]
            gaps.extend(f"{reference}:{key}" for key in sorted(keys) if key not in current_by_key)
        row["bindings"] = bindings
        if gaps:
            row["binding_gaps"] = gaps
        else:
            row.pop("binding_gaps", None)


def _known_source_lines(catalog: dict[str, Any], reference: str) -> dict[str, int]:
    lines: dict[str, int] = {}
    for row in catalog.get("selectors", {}).values():
        for field in ("sources", "bindings"):
            for entry in row.get(field, {}).get(reference, []):
                if entry.get("key") and isinstance(entry.get("line"), int):
                    lines[entry["key"]] = entry["line"]
    for row in catalog.get("upstream_consensus", []):
        for entry in row.get("sources", {}).get(reference, []):
            if entry.get("key") and isinstance(entry.get("line"), int):
                lines[entry["key"]] = entry["line"]
    return lines


def _preserve_lines(items: list[SourceSelector], known: dict[str, int]) -> list[SourceSelector]:
    return [
        SourceSelector(
            item.value, item.normalized, item.file, known.get(item.key, item.line), item.key
        )
        for item in items
    ]


def _upstream_consensus(
    indexes: dict[str, list[SourceSelector]],
    previous: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: defaultdict[str, dict[str, list[SourceSelector]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for reference, items in indexes.items():
        for item in items:
            grouped[item.normalized][reference].append(item)
    previous_by_value = {
        normalize_selector(row["value"]): row for row in previous or () if row.get("value")
    }
    rows: list[dict[str, Any]] = []
    for _normalized, by_reference in sorted(grouped.items()):
        if len(by_reference) < 2:
            continue
        first = next(iter(next(iter(by_reference.values()))))
        row = {
            "value": first.value,
            "references": sorted(by_reference),
            "sources": {
                name: [_source_dict(item) for item in items[:8]]
                for name, items in sorted(by_reference.items())
            },
        }
        old = previous_by_value.get(_normalized)
        if old:
            for field in (
                "decision",
                "logical_id",
                "origin",
                "verification",
                "reason_code",
                "reason",
                "target",
                "evidence",
                "last_verified_at",
                "verified_flow",
                "verified_by",
            ):
                if field in old:
                    row[field] = copy.deepcopy(old[field])
        rows.append(row)
    return rows


def _ensure_extra_contracts(catalog: dict[str, Any], evidence: dict[str, Any]) -> None:
    for logical_id, contract in EXTRA_CONTRACTS.items():
        if logical_id in catalog["selectors"]:
            continue
        value = contract["value"]
        live_matches = _selector_live_matches(value, evidence)
        decision = "live_dom" if live_matches else contract.get("decision", "unavailable")
        row: dict[str, Any] = {
            "value": value,
            "active": decision != "unavailable",
            "criticality": contract["criticality"],
            "declared_at": contract["declared_at"],
            "decision": decision,
            "sources": {},
            "live_matches": live_matches,
        }
        if decision in {"documented_live", "workflow_live"}:
            row["evidence"] = {
                "source": contract["declared_at"],
                "note": "reviewed existing runtime selector and its read-only/live workflow",
            }
        catalog["selectors"][logical_id] = row


def build_map(reference_root: Path, live_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata, indexes = _reference_indexes(reference_root)
    evidence = build_live_evidence(live_root)
    selectors: dict[str, Any] = {}
    for logical_id, discovered in sorted(discover_declared_selectors().items()):
        value = discovered["value"]
        sources = {
            name: matches
            for name, items in indexes.items()
            if (matches := _matching_sources(value, items))
        }
        live_matches = _selector_live_matches(value, evidence)
        reference_count = len(sources)
        if reference_count >= 2:
            decision = "consensus"
        elif live_matches:
            decision = "live_dom"
        elif documentation := documented_evidence(discovered["declared_at"]):
            decision = "documented_live"
        else:
            decision = "unavailable"
        special = SPECIAL_POLICIES.get(logical_id)
        if special and decision == "unavailable":
            decision = special[0]
        selectors[logical_id] = {
            "value": value,
            "active": decision != "unavailable",
            "criticality": discovered["criticality"],
            "declared_at": discovered["declared_at"],
            "decision": decision,
            "sources": sources,
            "live_matches": live_matches,
        }
        if decision == "documented_live":
            selectors[logical_id]["evidence"] = documentation or {
                "source": discovered["declared_at"],
                "note": SPECIAL_POLICIES[logical_id][1],
            }
        elif special and decision == special[0]:
            selectors[logical_id]["evidence"] = {
                "source": discovered["declared_at"],
                "note": special[1],
            }
    catalog = {
        "version": 1,
        "policy": {"mode": "manual", "consensus_threshold": 2},
        "references": metadata,
        "binding_definitions": _binding_definitions(),
        "upstream_consensus": _upstream_consensus(indexes),
        "selectors": selectors,
    }
    _ensure_extra_contracts(catalog, evidence)
    _refresh_bindings(catalog, indexes)
    catalog["selectors"] = dict(sorted(catalog["selectors"].items()))
    return catalog, evidence


def load_catalog() -> dict[str, Any]:
    if not MAP_PATH.exists():
        raise SystemExit(f"selector map missing: {MAP_PATH}")
    catalog = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    evidence = (
        json.loads(EVIDENCE_PATH.read_text(encoding="utf-8")) if EVIDENCE_PATH.exists() else {}
    )
    _ensure_extra_contracts(catalog, evidence)
    catalog["selectors"] = dict(sorted(catalog["selectors"].items()))
    for logical_id, row in catalog.get("selectors", {}).items():
        row["criticality"] = classify_criticality(logical_id)
        row.setdefault("active", row.get("decision") != "unavailable")
    return catalog


def load_baseline() -> dict[str, Any]:
    """Load the frozen Issue 599 scope characterization."""
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def affected_logical_ids(catalog: dict[str, Any]) -> dict[str, Any]:
    """Calculate drift classes once per logical ID, never per source row."""
    literal_mismatch_ids: set[str] = set()
    broken_binding_ids: set[str] = set()
    for logical_id, row in catalog.get("selectors", {}).items():
        canonical = normalize_selector(row.get("value", ""))
        if any(
            entries
            and not any(
                normalize_selector(entry.get("value", "")) == canonical for entry in entries
            )
            for entries in row.get("bindings", {}).values()
        ):
            literal_mismatch_ids.add(logical_id)
        if row.get("binding_gaps"):
            broken_binding_ids.add(logical_id)

    overlap_ids = literal_mismatch_ids & broken_binding_ids
    affected_ids = literal_mismatch_ids | broken_binding_ids
    return {
        "literal_mismatch_ids": sorted(literal_mismatch_ids),
        "broken_binding_ids": sorted(broken_binding_ids),
        "overlap_ids": sorted(overlap_ids),
        "affected_unique_ids": sorted(affected_ids),
        "literal_mismatches": len(literal_mismatch_ids),
        "broken_bindings": len(broken_binding_ids),
        "overlap": len(overlap_ids),
        "affected_unique": len(affected_ids),
    }


def write_catalog(catalog: dict[str, Any], evidence: dict[str, Any] | None = None) -> None:
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(
        yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
    )
    if evidence is not None:
        EVIDENCE_PATH.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    GENERATED_PATH.write_text(render_generated(catalog), encoding="utf-8")
    MATRIX_PATH.write_text(render_matrix(catalog), encoding="utf-8")


def render_generated(catalog: dict[str, Any]) -> str:
    values = {
        key: row["value"]
        for key, row in catalog["selectors"].items()
        if row.get("active", row["decision"] != "unavailable")
    }
    rendered = json.dumps(values, ensure_ascii=False, indent=4, sort_keys=True)
    return (
        '"""Generated by scripts/selector_contracts.py; do not edit by hand."""\n\n'
        "# ruff: noqa: E501\n\n"
        "from __future__ import annotations\n\n"
        "# fmt: off\n"
        f"VALUES: dict[str, str] = {rendered}\n"
        "# fmt: on\n\n\n"
        "def selector(logical_id: str) -> str:\n"
        "    try:\n"
        "        return VALUES[logical_id]\n"
        "    except KeyError as exc:\n"
        '        raise RuntimeError(f"selector is unavailable: {logical_id}") from exc\n\n\n'
        "def optional_selector(logical_id: str) -> str | None:\n"
        "    return VALUES.get(logical_id)\n"
    )


def _markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_matrix(catalog: dict[str, Any]) -> str:
    lines = [
        "# Selector reference matrix",
        "",
        "> Generated by `scripts/selector_contracts.py`; do not edit by hand.",
        "",
        "| logical id | ours | Steev | tgeruzov | YAMAKAYAMACO | verdict |",
        "|---|---|---|---|---|---|",
    ]
    for logical_id, row in catalog["selectors"].items():
        source_values = {}
        for name in REFERENCE_CONFIG:
            values = [item["value"] for item in row.get("bindings", {}).get(name, [])]
            source_values[name] = "<br>".join(dict.fromkeys(values)) or "—"
        lines.append(
            "| "
            + " | ".join(
                _markdown(value)
                for value in (
                    logical_id,
                    f"`{row['value']}`",
                    source_values["steev"],
                    source_values["tgeruzov"],
                    source_values["yamakayama"],
                    row["decision"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Raw 2-of-3 reference consensus",
            "",
            "These rows are extracted from the approved upstream projects "
            "even when hhru does not use them yet.",
            "",
            "| selector | references |",
            "|---|---|",
        ]
    )
    for row in catalog.get("upstream_consensus", []):
        selector_value = _markdown(f"`{row['value']}`")
        references = _markdown(", ".join(row["references"]))
        lines.append(f"| {selector_value} | {references} |")
    decisions = [
        row
        for row in catalog.get("upstream_consensus", [])
        if _is_apply_response_candidate(row.get("value", "")) and row.get("decision")
    ]
    if decisions:
        lines.extend(
            [
                "",
                "## Apply/response candidate decisions",
                "",
                "Every apply/response candidate is explicitly resolved; rejected "
                "WRITE candidates are not added as fallback selectors.",
                "",
                "| selector | decision | target | reason |",
                "|---|---|---|---|",
            ]
        )
        for row in decisions:
            cells = (
                f"`{row['value']}`",
                row.get("decision", "—"),
                row.get("target", "—"),
                row.get("reason", "—"),
            )
            lines.append("| " + " | ".join(_markdown(value or "—") for value in cells) + " |")
    return "\n".join(lines) + "\n"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not body or not isinstance(body, list):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            result.add(id(first.value))
    return result


def unmanaged_selector_literals(catalog: dict[str, Any] | None = None) -> list[str]:
    """Find selector literals that are not represented by the catalog.

    Existing consumers may still keep a compatibility literal while migrating
    to generated groups.  A literal already represented by a catalog row is
    therefore managed; genuinely new literals remain a hard failure.
    """
    managed = {
        normalize_selector(row["value"])
        for row in (catalog or {}).get("selectors", {}).values()
        if row.get("value")
    }
    findings: list[str] = []
    selector_group_root = SOURCE_ROOT / "selector_groups"
    for path in sorted(selector_group_root.rglob("*.py")):
        if path == GENERATED_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or not _contains_selector(node.value):
                continue
            if any(
                normalize_selector(candidate) in managed
                for candidate in _selector_candidates(node.value)
            ):
                continue
            parent = parents.get(id(node))
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute):
                if parent.func.attr == "compile":
                    continue
            findings.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:120]!r}")
    return findings


def _candidate_decision_errors(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for row in catalog.get("upstream_consensus", []):
        if not _is_apply_response_candidate(row.get("value", "")):
            continue
        decision = row.get("decision")
        if decision not in {"port_exact", "reject", "unavailable"}:
            errors.append(
                f"upstream candidate {row.get('value', '')}: "
                "must have port_exact, reject, or unavailable decision"
            )
            continue
        required_fields = (
            "origin",
            "verification",
            "evidence",
            "last_verified_at",
            "verified_flow",
            "verified_by",
        )
        for field in required_fields:
            if not row.get(field):
                errors.append(f"upstream candidate {row.get('value', '')}: missing {field}")
        evidence = row.get("evidence", {})
        if not evidence.get("source") or not evidence.get("note"):
            errors.append(f"upstream candidate {row.get('value', '')}: evidence is incomplete")
        if decision in {"reject", "unavailable"} and not row.get("reason"):
            errors.append(f"upstream candidate {row.get('value', '')}: missing reject reason")
        if decision == "port_exact" and not row.get("target"):
            errors.append(f"upstream candidate {row.get('value', '')}: missing target contract")
    return errors


def verify_catalog(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = _candidate_decision_errors(catalog)
    threshold = int(catalog.get("policy", {}).get("consensus_threshold", 2))
    if threshold != 2:
        errors.append(f"consensus_threshold must be 2, got {threshold}")
    if set(catalog.get("references", {})) != set(REFERENCE_CONFIG):
        errors.append("reference set differs from the approved three-project allowlist")
    if catalog.get("binding_definitions") != _binding_definitions():
        errors.append("semantic reference bindings are stale; run selector refresh")
    for logical_id, row in catalog.get("selectors", {}).items():
        sources = row.get("sources", {})
        reference_count = len(sources)
        decision = row.get("decision")
        if decision in {"drift_pending", "drift_conflict"}:
            errors.append(f"{logical_id}: unresolved upstream selector {decision}")
        if decision == "consensus" and reference_count < threshold:
            errors.append(f"{logical_id}: consensus has only {reference_count} references")
        if decision == "live_dom" and not row.get("live_matches"):
            errors.append(f"{logical_id}: live_dom has no sanitized DOM match")
        if decision == "documented_live":
            evidence = row.get("evidence", {})
            source = evidence.get("source", "")
            if (
                not source
                or not evidence.get("note")
                or not (ROOT / source.rsplit(":", 1)[0]).is_file()
            ):
                errors.append(f"{logical_id}: documented_live evidence is incomplete")
        if decision in {"safety_guard", "structural_read_fallback", "workflow_live"}:
            evidence = row.get("evidence", {})
            if not evidence.get("source") or not evidence.get("note"):
                errors.append(f"{logical_id}: {decision} evidence is incomplete")
            if decision == "structural_read_fallback" and row.get("criticality") != "read":
                errors.append(f"{logical_id}: structural read fallback is classified as WRITE")
        if decision == "unavailable" and row.get("active", True):
            errors.append(f"{logical_id}: selector is unavailable and must not be used")
        for name, items in sources.items():
            if name not in REFERENCE_CONFIG:
                errors.append(f"{logical_id}: unapproved reference {name}")
            if not any(
                normalize_selector(item["value"]) == normalize_selector(row["value"])
                for item in items
            ):
                errors.append(f"{logical_id}: {name} source does not equal canonical selector")
    errors.extend(
        f"unmanaged selector: {finding}" for finding in unmanaged_selector_literals(catalog)
    )
    expected_generated = render_generated(catalog)
    if (
        not GENERATED_PATH.exists()
        or GENERATED_PATH.read_text(encoding="utf-8") != expected_generated
    ):
        errors.append(f"generated runtime is stale: run {Path(__file__).relative_to(ROOT)} render")
    expected_matrix = render_matrix(catalog)
    if not MATRIX_PATH.exists() or MATRIX_PATH.read_text(encoding="utf-8") != expected_matrix:
        errors.append(f"reference matrix is stale: run {Path(__file__).relative_to(ROOT)} render")
    return errors


def _tracked_consensus(
    row: dict[str, Any], indexes: dict[str, list[SourceSelector]]
) -> tuple[str, dict[str, list[dict[str, Any]]]] | None:
    candidates: defaultdict[str, dict[str, list[SourceSelector]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for reference, old_sources in row.get("bindings", row.get("sources", {})).items():
        current_by_key = {item.key: item for item in indexes.get(reference, [])}
        for old_source in old_sources:
            if item := current_by_key.get(old_source.get("key", "")):
                candidates[item.normalized][reference].append(item)
    viable = [item for item in candidates.items() if len(item[1]) >= 2]
    if not viable:
        return None
    _, by_reference = max(viable, key=lambda item: len(item[1]))
    first = next(iter(next(iter(by_reference.values()))))
    return first.value, {
        name: [_source_dict(item) for item in items[:8]]
        for name, items in sorted(by_reference.items())
    }


def refresh_catalog(reference_root: Path, mode: str | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    previous_consensus = copy.deepcopy(catalog.get("upstream_consensus", []))
    metadata, indexes = _reference_indexes(reference_root)
    old_metadata = catalog.get("references", {})
    unchanged = {
        name: old_metadata.get(name, {}).get("selector_fingerprint")
        == current.get("selector_fingerprint")
        for name, current in metadata.items()
    }
    for name, is_unchanged in unchanged.items():
        if is_unchanged:
            indexes[name] = _preserve_lines(indexes[name], _known_source_lines(catalog, name))
    catalog["references"] = {
        name: old_metadata[name] if unchanged[name] else current
        for name, current in metadata.items()
    }
    if mode is not None:
        catalog["policy"]["mode"] = mode
    catalog["binding_definitions"] = _binding_definitions()
    catalog["upstream_consensus"] = _upstream_consensus(indexes, previous_consensus)
    _invalidate_changed_candidate_verification(catalog, previous_consensus, unchanged)
    _refresh_bindings(catalog, indexes)
    for row in catalog["selectors"].values():
        audit_unverified = (
            row.get("status") == "needs-live-evidence" or row.get("verification") == "unverified"
        )
        previous_decision = row.get("decision", "unavailable")
        previous_active = row.get("active", False)
        current_value = row["value"]
        tracked = _tracked_consensus(row, indexes)
        if tracked and normalize_selector(tracked[0]) != normalize_selector(current_value):
            candidate_value, candidate_sources = tracked
            if catalog["policy"].get("mode") == "read_auto" and row["criticality"] == "read":
                row["value"] = candidate_value
                row.pop("suggestion", None)
            else:
                row["suggestion"] = {
                    "value": candidate_value,
                    "sources": candidate_sources,
                    "reason": "same source keys now agree on a different selector",
                }
                row["decision"] = "drift_pending"
                row["active"] = True
        value = row["value"]
        row["sources"] = {
            name: matches
            for name, items in indexes.items()
            if (matches := _matching_sources(value, items))
        }
        reference_count = len(row["sources"])
        if audit_unverified:
            # Audit metadata must not become activation evidence.  In
            # particular, do not let a stale match activate a selector that was
            # unavailable.  Preserve an already-active contract so a refresh
            # cannot remove a mandatory import from the generated runtime.
            row["decision"] = previous_decision
            row["active"] = previous_active
            row.pop("suggestion", None)
        elif reference_count >= catalog["policy"]["consensus_threshold"]:
            row["decision"] = "consensus"
            row["active"] = True
            row.pop("suggestion", None)
        elif row.get("decision") == "drift_pending":
            pass
        elif row.get("live_matches"):
            row["decision"] = "live_dom"
            row["active"] = True
        elif row.get("evidence"):
            # Preserve reviewed non-consensus evidence classes across refresh.
            previous = row.get("decision")
            row["decision"] = (
                previous
                if previous
                in {
                    "documented_live",
                    "safety_guard",
                    "structural_read_fallback",
                    "workflow_live",
                }
                else "documented_live"
            )
            row["active"] = True
        elif row.get("sources") and row.get("active", False):
            row["decision"] = "drift_conflict"
            row["active"] = False
        else:
            row["decision"] = "unavailable"
            row["active"] = False
        if row.get("binding_gaps"):
            row["decision"] = "drift_conflict"
            row["active"] = False
    return catalog


def _short(value: str, limit: int = 140) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _is_apply_response_candidate(value: str) -> bool:
    return "vacancy-response-" in value or "vacancy-serp__vacancy-employer" in value


def _invalidate_changed_candidate_verification(
    catalog: dict[str, Any],
    previous_rows: list[dict[str, Any]],
    unchanged_references: dict[str, bool],
) -> None:
    previous_by_value = {
        normalize_selector(row.get("value", "")): row for row in previous_rows if row.get("value")
    }
    for row in catalog.get("upstream_consensus", []):
        if not _is_apply_response_candidate(row.get("value", "")) or not row.get("decision"):
            continue
        previous = previous_by_value.get(normalize_selector(row["value"]))
        if not previous:
            continue
        provenance_changed = (
            previous.get("references") != row.get("references")
            or previous.get("sources") != row.get("sources")
            or any(not unchanged_references.get(name, False) for name in row["references"])
        )
        if not provenance_changed:
            continue
        row["verification"] = "unverified"
        row["evidence"] = {
            "source": "selectors/reference-map.yaml:upstream_consensus",
            "note": (
                "Upstream provenance changed; the prior candidate decision is retained, "
                "but manual re-review is required."
            ),
            "reference_commits": {
                name: catalog["references"][name]["commit"] for name in row["references"]
            },
        }
        row["verified_by"] = "ci"


def render_refresh_report(before: dict[str, Any], after: dict[str, Any]) -> str:
    before_rows = before.get("selectors", {})
    after_rows = after.get("selectors", {})
    canonical_changes = [
        logical_id
        for logical_id, row in after_rows.items()
        if before_rows.get(logical_id, {}).get("value") != row.get("value")
    ]
    decision_changes = [
        logical_id
        for logical_id, row in after_rows.items()
        if before_rows.get(logical_id, {}).get("decision") != row.get("decision")
    ]
    suggestions = [logical_id for logical_id, row in after_rows.items() if row.get("suggestion")]
    reference_changes = [
        name
        for name, metadata in after.get("references", {}).items()
        if before.get("references", {}).get(name, {}).get("selector_fingerprint")
        != metadata.get("selector_fingerprint")
    ]

    bound_rows = 0
    divergent: list[tuple[str, dict[str, list[str]]]] = []
    for logical_id, row in after_rows.items():
        bindings = row.get("bindings", {})
        if not bindings:
            continue
        bound_rows += 1
        canonical = normalize_selector(row["value"])
        mismatches = {
            reference: list(dict.fromkeys(item["value"] for item in entries))
            for reference, entries in bindings.items()
            if entries
            and not any(normalize_selector(item["value"]) == canonical for item in entries)
        }
        if mismatches:
            divergent.append((logical_id, mismatches))

    canonical_values = {normalize_selector(row["value"]) for row in after_rows.values()}
    unused_consensus = [
        row
        for row in after.get("upstream_consensus", [])
        if normalize_selector(row["value"]) not in canonical_values
    ]
    unavailable = [
        logical_id for logical_id, row in after_rows.items() if not row.get("active", True)
    ]
    decisions: defaultdict[str, int] = defaultdict(int)
    for row in after_rows.values():
        decisions[row.get("decision", "unknown")] += 1

    affected = affected_logical_ids(after)
    baseline = load_baseline()
    baseline_scope = baseline["scope"]

    lines = [
        "selector refresh DRY-RUN — no files, branches, or PRs were written",
        "",
        "Delta since the stored map:",
        f"  reference selector sets changed: {len(reference_changes)}"
        + (f" ({', '.join(reference_changes)})" if reference_changes else ""),
        f"  canonical selector changes: {len(canonical_changes)}",
        f"  decision changes: {len(decision_changes)}",
        f"  unresolved suggestions: {len(suggestions)}",
        "",
        "Current three-project baseline:",
        f"  local selector contracts: {len(after_rows)}",
        f"  rows with semantic reference bindings: {bound_rows}",
        f"  rows with at least one bound reference mismatch: {len(divergent)}",
        f"  rows absent from all semantic reference bindings: {len(after_rows) - bound_rows}",
        f"  raw 2-of-3 upstream selectors unused locally: {len(unused_consensus)}",
        f"  unavailable/fail-closed selectors: {len(unavailable)}",
        "  decisions: " + ", ".join(f"{name}={count}" for name, count in sorted(decisions.items())),
        "",
        "Issue 599 affected logical IDs:",
        f"  literal_mismatches={affected['literal_mismatches']}",
        f"  broken_bindings={affected['broken_bindings']}",
        f"  overlap={affected['overlap']}",
        f"  affected_unique={affected['affected_unique']}",
        "  ids: " + ", ".join(affected["affected_unique_ids"]),
        "  frozen_baseline_delta: " + ("same" if affected == baseline_scope else "CHANGED"),
    ]
    if divergent:
        lines.extend(["", "Semantic mismatches:"])
        for logical_id, mismatches in divergent:
            ours = _short(after_rows[logical_id]["value"])
            rendered = "; ".join(
                f"{reference}={_short(' || '.join(values))}"
                for reference, values in sorted(mismatches.items())
            )
            lines.append(f"  [DIFF] {logical_id}: ours={ours}; {rendered}")
    if unused_consensus:
        lines.extend(["", "Unused upstream 2-of-3 consensus:"])
        for row in unused_consensus:
            lines.append(f"  [UPSTREAM] {_short(row['value'])} ({', '.join(row['references'])})")
    if unavailable:
        lines.extend(["", "Fail-closed selectors:"])
        lines.extend(f"  [OFF] {logical_id}" for logical_id in unavailable)
    machine = {
        "report": "selector-refresh",
        "dry_run": True,
        "issue": 599,
        "affected": affected,
        "baseline": baseline_scope,
    }
    lines.extend(
        [
            "",
            "MACHINE_READABLE_JSON:",
            json.dumps(machine, ensure_ascii=False, sort_keys=True),
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--reference-root", type=Path, required=True)
    bootstrap.add_argument("--live-root", type=Path, default=ROOT / "data" / "logs")
    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--reference-root", type=Path, required=True)
    refresh.add_argument("--mode", choices=("manual", "read_auto"))
    refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="print baseline/drift report without writing files",
    )
    subparsers.add_parser("render")
    subparsers.add_parser("check")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "bootstrap":
        catalog, evidence = build_map(args.reference_root, args.live_root)
        candidate_errors = _candidate_decision_errors(catalog)
        if candidate_errors:
            print(
                "bootstrap refused: resolve apply/response candidates before writing the catalog",
                file=sys.stderr,
            )
            for error in candidate_errors:
                print(f"  {error}", file=sys.stderr)
            return 1
        write_catalog(catalog, evidence)
        unavailable = sum(row["decision"] == "unavailable" for row in catalog["selectors"].values())
        print(f"wrote {len(catalog['selectors'])} selector contracts; unavailable={unavailable}")
        return 0
    if args.command == "refresh":
        before = copy.deepcopy(load_catalog())
        catalog = refresh_catalog(args.reference_root, args.mode)
        if args.dry_run:
            print(render_refresh_report(before, catalog), end="")
            return 0
        write_catalog(catalog)
        print("selector references refreshed")
        return 0
    if args.command == "render":
        write_catalog(load_catalog())
        return 0
    catalog = load_catalog()
    errors = verify_catalog(catalog)
    if errors:
        print("selector contract check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"selector contracts OK: {len(catalog['selectors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
