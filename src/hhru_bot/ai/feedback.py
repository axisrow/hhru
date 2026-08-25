"""Prompt adapters for persisted vacancy feedback (#592)."""

from __future__ import annotations

FEEDBACK_CONTEXT_MAX_CHARS = 4000
FEEDBACK_RECENT_TAIL = 25
REJECT_CONTEXT_MAX_ITEMS = 12
STYLE_CONTEXT_MAX_ITEMS = 6

_REJECT_HEADER = "Ранее вы отклоняли вакансии по таким причинам (учти при оценке):\n"
_STYLE_HEADER = (
    "Фрагменты после правок пользователя — учитывай только стиль и формулировки, "
    "не копируй факты:\n"
)


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bounded_block(header: str, items: list[str], max_chars: int) -> str:
    """Join recent items under one deterministic character budget."""
    if not items or max_chars <= len(header):
        return ""
    parts: list[str] = []
    used = len(header)
    for item in items:
        chunk = f"- {item}\n"
        if used + len(chunk) > max_chars:
            remaining = max_chars - used - 4
            if remaining >= 1:
                parts.append(f"- {item[:remaining]}…\n")
            break
        parts.append(chunk)
        used += len(chunk)
    return (header + "".join(parts)).rstrip() if parts else ""


def build_reject_context(rows: list[dict], *, max_chars: int = FEEDBACK_CONTEXT_MAX_CHARS) -> str:
    """Build the scoring-only narrative from recent rejects with reasons."""
    recent = rows[:FEEDBACK_RECENT_TAIL]
    newest = [
        _text(row, "reason")
        for row in recent
        if row.get("action") == "reject" and _text(row, "reason")
    ][:REJECT_CONTEXT_MAX_ITEMS]
    return _bounded_block(_REJECT_HEADER, list(reversed(newest)), max_chars)


def build_style_context(rows: list[dict], *, max_chars: int = FEEDBACK_CONTEXT_MAX_CHARS) -> str:
    """Build the letter-only style block from already-redacted edit snippets."""
    recent = rows[:FEEDBACK_RECENT_TAIL]
    newest = [_text(row, "edited_snippet") for row in recent if _text(row, "edited_snippet")][
        :STYLE_CONTEXT_MAX_ITEMS
    ]
    return _bounded_block(_STYLE_HEADER, list(reversed(newest)), max_chars)
