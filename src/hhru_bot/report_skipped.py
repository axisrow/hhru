"""Форматтер журнала отсева для команды skipped (#392)."""

from __future__ import annotations

from collections.abc import Iterable

from .report import _ascii_table

_COLUMNS = ("created_at", "resume_id", "vacancy_id", "title", "company", "reason", "search_query")
_HEADERS = ("Время", "Резюме", "Вакансия", "Название", "Компания", "Причина", "Поиск")


def format_skipped(rows: Iterable[dict]) -> str:
    """Рисует журнал skipped; пустой журнал всё равно показывает шапку."""
    body = [[str(row.get(column) or "") for column in _COLUMNS] for row in rows]
    return _ascii_table(list(_HEADERS), body)
