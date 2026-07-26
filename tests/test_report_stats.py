"""Characterization-тесты форматтеров отчётов команды stats (#11).

Проверяют format_summary / format_actions в новом report.py для table|csv|md:
пустые данные не падают, ключи/значения присутствуют, эмодзи нет. Без браузера.
"""

from __future__ import annotations

import csv
import io

from hhru_bot.report import format_actions, format_summary

_NO_EMOJI = set(
    chr(c)
    for c in range(0x1F000, 0x1FAFF + 1)  # эмодзи/разные символы и пиктограммы
) | set(chr(c) for c in range(0x2600, 0x27BF + 1))


def _has_emoji(text: str) -> bool:
    return any(ch in _NO_EMOJI for ch in text)


def _empty_summary() -> dict:
    return {
        "apply": {"success": 0, "dry_run": 0, "failed": 0},
        "bump": {"success": 0, "dry_run": 0, "failed": 0},
        "total": 0,
    }


def _filled_summary() -> dict:
    s = _empty_summary()
    s["apply"]["success"] = 2
    s["apply"]["dry_run"] = 1
    s["apply"]["failed"] = 1
    s["bump"]["success"] = 3
    s["total"] = 7
    return s


def _sample_actions() -> list[dict]:
    return [
        {
            "resume_id": "r1",
            "vacancy_id": "v2",
            "action": "apply",
            "status": "failed",
            "reason": "captcha",
            "created_at": "2026-07-27T10:00:00",
        },
        {
            "resume_id": "r1",
            "vacancy_id": "v1",
            "action": "apply",
            "status": "success",
            "reason": None,
            "created_at": "2026-07-27T09:00:00",
        },
    ]


# --- format_summary ---------------------------------------------------------


def test_format_summary_empty_all_formats_do_not_crash():
    for fmt in ("table", "csv", "md"):
        out = format_summary(_empty_summary(), fmt)
        assert isinstance(out, str)
        assert _has_emoji(out) is False


def test_format_summary_table_has_action_columns_and_zero_total():
    out = format_summary(_empty_summary(), "table")
    assert "apply" in out
    assert "bump" in out
    # человекочитаемые подписи статусов (table/md — для людей)
    assert "Успех" in out
    assert "0" in out  # нули видны
    # ASCII-разделители таблицы
    assert "+" in out and "-" in out


def test_format_summary_table_shows_counts():
    out = format_summary(_filled_summary(), "table")
    assert "2" in out  # apply success
    assert "3" in out  # bump success
    assert "7" in out  # total


def test_format_summary_csv_parses_as_csv():
    out = format_summary(_filled_summary(), "csv")
    reader = csv.reader(io.StringIO(out))
    rows = list(reader)
    assert len(rows) >= 1
    header = rows[0]
    # в заголовке есть что-то осмысленное про action/status
    flat_header = ",".join(header).lower()
    assert "success" in flat_header


def test_format_summary_md_has_pipe_table():
    out = format_summary(_filled_summary(), "md")
    assert "|" in out
    # markdown-разделитель строки
    assert "---" in out


# --- format_actions ---------------------------------------------------------


def test_format_actions_empty_all_formats():
    for fmt in ("table", "csv", "md"):
        out = format_actions([], fmt)
        assert isinstance(out, str)
        assert _has_emoji(out) is False


def test_format_actions_table_contains_rows_and_columns():
    out = format_actions(_sample_actions(), "table")
    assert "v2" in out
    assert "v1" in out
    assert "apply" in out
    assert "success" in out
    assert "failed" in out
    assert "+" in out  # рамка ASCII


def test_format_actions_csv_parses_and_has_header():
    out = format_actions(_sample_actions(), "csv")
    reader = csv.reader(io.StringIO(out))
    rows = list(reader)
    header = rows[0]
    assert "vacancy_id" in header
    assert "status" in header
    # данные: 2 строки + заголовок
    assert len(rows) == 3
    assert rows[1][header.index("vacancy_id")] == "v2"
    assert rows[1][header.index("status")] == "failed"


def test_format_actions_md_is_pipe_table():
    out = format_actions(_sample_actions(), "md")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # есть строка-разделитель markdown: ячейки состоят только из '-'
    def is_separator(ln: str) -> bool:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        return bool(cells) and all(set(c) == {"-"} and c for c in cells)

    assert any(is_separator(ln) for ln in lines)
    assert "vacancy_id" in out or "Вакансия" in out


def test_format_actions_csv_quotes_comma_in_reason():
    rows = [
        {
            "resume_id": "r1",
            "vacancy_id": "v1",
            "action": "apply",
            "status": "failed",
            "reason": "ошибка, с запятой",
            "created_at": "2026-07-27T10:00:00",
        }
    ]
    out = format_actions(rows, "csv")
    parsed = list(csv.reader(io.StringIO(out)))
    header = parsed[0]
    reason_col = header.index("reason")
    # значение восстанавливается корректно (csv-модуль справился с запятой)
    assert parsed[1][reason_col] == "ошибка, с запятой"


def test_unknown_format_raises():
    import pytest

    with pytest.raises(ValueError):
        format_summary(_empty_summary(), "xml")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_actions([], "xml")  # type: ignore[arg-type]
