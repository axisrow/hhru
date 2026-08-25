from __future__ import annotations

FEEDBACK_BUDGET = 4000
FEEDBACK_TAIL = 20


def feedback_blocks(rows: list[dict], *, budget: int = FEEDBACK_BUDGET) -> tuple[str, str]:
    """Build bounded reject and style context; rows are scoped to one resume."""
    recent = rows[:FEEDBACK_TAIL]
    rejects = [r for r in recent if r.get("action") == "reject" and r.get("reason")]
    styles = [r for r in recent if r.get("edited_snippet")]
    blocks = [
        "Причины прошлых отклонений:\n" + "\n".join(f"- {r['reason']}" for r in reversed(rejects))
        if rejects
        else "",
        "Правки прошлых писем (учитывай как стиль, не копируй факты):\n"
        + "\n".join(f"- {r['edited_snippet']}" for r in reversed(styles))
        if styles
        else "",
    ]
    result = []
    used = 0
    for block in blocks:
        if block and used < budget:
            value = block[: budget - used]
            result.append(value)
            used += len(value)
        else:
            result.append("")
    return tuple(result)  # type: ignore[return-value]
