"""Форматтеры отчётов для команды stats (#11).

Владелец файла — #11 (команда stats). Один report-топик на файл (конвенция
CLAUDE.md), поэтому воронка/фаннел живут отдельно в report_funnel.py.

Только текст/ASCII-таблицы — НИКАКИХ эмодзи (правило проекта: CLI-вывод чистый).
Экспорт идёт в stdout (команда stats печатает результат этих функций).
Поддерживаемые форматы: table, csv, md.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

SUPPORTED_FORMATS = ("table", "csv", "md")

# Порядок колонок сводки по action.
_SUMMARY_ACTIONS = ("apply", "bump")

# Порядок колонок таблицы действий.
_ACTION_COLUMNS = (
    "created_at",
    "resume_id",
    "action",
    "vacancy_id",
    "status",
    "reason",
)

_ACTION_HEADERS = {
    "created_at": "Время",
    "resume_id": "Резюме",
    "action": "Действие",
    "vacancy_id": "Вакансия",
    "status": "Статус",
    "reason": "Причина",
}

_SUMMARY_HEADERS = {
    "action": "Действие",
    "success": "Успех",
    "dry_run": "Прогон",
    "failed": "Провал",
}


def _check_format(fmt: str) -> None:
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Неизвестный формат вывода: {fmt!r}. Допустимо: {', '.join(SUPPORTED_FORMATS)}"
        )


# --- summary ----------------------------------------------------------------


def _summary_rows(summary: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for action in _SUMMARY_ACTIONS:
        bucket = summary.get(action, {})
        rows.append(
            [
                action,
                str(bucket.get("success", 0)),
                str(bucket.get("dry_run", 0)),
                str(bucket.get("failed", 0)),
            ]
        )
    return rows


def format_summary(summary: dict, fmt: str) -> str:
    """Отрисовать сводку action × status (+ total) в выбранном формате."""
    _check_format(fmt)
    header = [
        _SUMMARY_HEADERS["action"],
        _SUMMARY_HEADERS["success"],
        _SUMMARY_HEADERS["dry_run"],
        _SUMMARY_HEADERS["failed"],
    ]
    rows = _summary_rows(summary)
    total = summary.get("total", 0)

    if fmt == "csv":
        # CSV — экспорт для машин: машиночитаемые имена колонок.
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["action", "success", "dry_run", "failed"])
        w.writerows(rows)
        w.writerow(["total", total, "", ""])
        return out.getvalue().rstrip("\n")

    if fmt == "md":
        lines = ["| " + " | ".join(header) + " |", "| --- | --- | --- | --- |"]
        for r in rows:
            lines.append("| " + " | ".join(r) + " |")
        lines.append(f"| **Итого** | {total} |  |  |")
        return "\n".join(lines)

    # table — ASCII
    return _ascii_table(header, rows, footer=["Итого", str(total), "", ""])


# --- actions ----------------------------------------------------------------


def _action_rows(rows: Iterable[dict]) -> list[list[str]]:
    out: list[list[str]] = []
    for r in rows:
        out.append([str(r.get(col) if r.get(col) is not None else "") for col in _ACTION_COLUMNS])
    return out


def format_actions(rows: Iterable[dict], fmt: str) -> str:
    """Отрисовать список действий (из History.list_actions) в выбранном формате."""
    _check_format(fmt)
    rows = list(rows)
    header = [_ACTION_HEADERS[c] for c in _ACTION_COLUMNS]

    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_ACTION_COLUMNS)  # машиночитаемые имена колонок для экспорта
        for r in rows:
            w.writerow([r.get(col) if r.get(col) is not None else "" for col in _ACTION_COLUMNS])
        return out.getvalue().rstrip("\n")

    if fmt == "md":
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        body = _action_rows(rows)
        for r in body:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)

    # table — ASCII
    if not rows:
        return _ascii_table(header, [])
    return _ascii_table(header, _action_rows(rows))


# --- ASCII-таблица ----------------------------------------------------------


def _ascii_table(header: list[str], rows: list[list[str]], footer: list[str] | None = None) -> str:
    """Простая ASCII-таблица с рамкой +---+. footer (если есть) — итоговая строка
    без попадания в основное тело. Пустое тело всё равно рисует шапку."""
    all_rows = [header, *rows]
    if footer is not None:
        all_rows = [header, *rows, footer]
    n_cols = len(header)
    widths = [0] * n_cols
    for r in all_rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def border() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(r: list[str]) -> str:
        cells = [
            str(r[i]).ljust(widths[i]) if i < len(r) else " " * widths[i] for i in range(n_cols)
        ]
        return "| " + " | ".join(cells) + " |"

    out = [border(), line(header), border()]
    for r in rows:
        out.append(line(r))
    if footer is not None:
        out.append(border())
        out.append(line(footer))
    out.append(border())
    return "\n".join(out)
