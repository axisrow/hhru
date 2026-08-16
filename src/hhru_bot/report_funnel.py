"""Форматтеры воронки для команды funnel (#13).

Отдельный файл от report.py — конвенция CLAUDE.md: «один report-топик на файл»
(report.py владеет #11/stats, здесь — воронка/фаннел #13).

Воронка: отправлено → просмотрено → приглашение → наш ответ → оффер, с конверсиями между
шагами. Плюс «мёртвая зона» (отклики без ответа старше N дней). Форматы table/md
(как в теле #13). Только текст/ASCII — НИКАКИХ эмодзи (правило CLI-вывода).
"""

from __future__ import annotations

from collections.abc import Iterable

# Только table/md — в ишью #13 воронка не требует CSV-экспорта (в отличие от
# stats #11, где CSV нужен для выгрузки сырых счётчиков).
SUPPORTED_FORMATS = ("table", "md")


def _check_format(fmt: str) -> None:
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Неизвестный формат вывода: {fmt!r}. Допустимо: {', '.join(SUPPORTED_FORMATS)}"
        )


# Шаги воронки + их человекочитаемые заголовки (table/md — для людей).
_FUNNEL_COLUMNS = (
    "resume_id",
    "sent",
    "viewed",
    "invited",
    "replied",
    "offer",
    "view_rate",
    "invite_rate",
    "reply_rate",
    "offer_rate",
)

_FUNNEL_HEADERS = {
    "resume_id": "Резюме",
    "sent": "Отправлено",
    "viewed": "Просмотрено",
    "invited": "Приглашение",
    "replied": "Наш ответ",
    "offer": "Оффер",
    "view_rate": "Просмотры %",
    "invite_rate": "Приглаш. %",
    "reply_rate": "Наш ответ %",
    "offer_rate": "Оффер %",
}

_DEAD_HEADERS = {
    "label": "Метрика",
    "value": "Значение",
}


def _fmt_rate(rate: float) -> str:
    """Конверсия → «NN.N%». Видна как процент, чтобы воронка читалась людьми."""
    return f"{rate:g}%"


def _funnel_rows(funnel: Iterable[dict]) -> list[list[str]]:
    out: list[list[str]] = []
    for r in funnel:
        out.append(
            [
                str(r.get("resume_id", "")),
                str(r.get("sent", 0)),
                str(r.get("viewed", 0)),
                str(r.get("invited", 0)),
                str(r.get("replied", 0)),
                str(r.get("offer", 0)),
                _fmt_rate(r.get("view_rate", 0.0)),
                _fmt_rate(r.get("invite_rate", 0.0)),
                _fmt_rate(r.get("reply_rate", 0.0)),
                _fmt_rate(r.get("offer_rate", 0.0)),
            ]
        )
    return out


def format_funnel(funnel: Iterable[dict], fmt: str) -> str:
    """Отрисовать воронку по резюме в выбранном формате (table/md).

    ``funnel`` — список строк из History.funnel_by_resume. Пустой список рисует
    шапку таблицы (нечего показать — но формат стабилен, не падает).
    """
    _check_format(fmt)
    rows = list(funnel)
    header = [_FUNNEL_HEADERS[c] for c in _FUNNEL_COLUMNS]

    if fmt == "md":
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        for r in _funnel_rows(rows):
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)

    # table — ASCII (переиспользуем рендер из report.py, чтобы рамка была единая)
    from .report import _ascii_table

    return _ascii_table(header, _funnel_rows(rows))


def format_dead(dead: dict, fmt: str) -> str:
    """Отрисовать «мёртвую зону» (доля откликов без ответа) в table/md.

    ``dead`` — {total_sent, dead, dead_rate} из History.dead_responses.
    """
    _check_format(fmt)
    total_sent = dead.get("total_sent", 0)
    dead_count = dead.get("dead", 0)
    dead_rate = dead.get("dead_rate", 0.0)

    rows = [
        ["Всего откликов (старше порога)", str(total_sent)],
        ["Без ответа («мёртвые»)", str(dead_count)],
        ["Доля без ответа", _fmt_rate(dead_rate)],
    ]
    header = [_DEAD_HEADERS["label"], _DEAD_HEADERS["value"]]

    if fmt == "md":
        lines = ["| " + " | ".join(header) + " |", "| --- | --- |"]
        for r in rows:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)

    from .report import _ascii_table

    return _ascii_table(header, rows)
