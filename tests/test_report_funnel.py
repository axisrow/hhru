"""Characterization-тесты форматтера воронки (#13).

report_funnel.format_funnel / format_dead — отдельный файл от report.py
(конвенция CLAUDE.md: один report-топик на файл). Поддержка table/md,
без эмодзи, деление на ноль (пустая воронка) не падает.
"""

from __future__ import annotations

from hhru_bot.report_funnel import format_dead, format_funnel

_NO_EMOJI = set(chr(c) for c in range(0x1F000, 0x1FAFF + 1)) | set(
    chr(c) for c in range(0x2600, 0x27BF + 1)
)


def _has_emoji(text: str) -> bool:
    return any(ch in _NO_EMOJI for ch in text)


def _empty_funnel() -> list[dict]:
    return []


def _filled_funnel() -> list[dict]:
    return [
        {
            "resume_id": "12345",
            "sent": 10,
            "viewed": 4,
            "invited": 2,
            "offer": 1,
            "view_rate": 40.0,
            "invite_rate": 50.0,
            "offer_rate": 50.0,
        },
        {
            "resume_id": "67890",
            "sent": 5,
            "viewed": 0,
            "invited": 0,
            "offer": 0,
            "view_rate": 0.0,
            "invite_rate": 0.0,
            "offer_rate": 0.0,
        },
    ]


# --- format_funnel ---------------------------------------------------------


def test_format_funnel_empty_all_formats_do_not_crash():
    for fmt in ("table", "md"):
        out = format_funnel(_empty_funnel(), fmt)
        assert isinstance(out, str)
        assert _has_emoji(out) is False


def test_format_funnel_table_has_columns_and_counts():
    out = format_funnel(_filled_funnel(), "table")
    # человекочитаемые заголовки шагов воронки
    for label in ("Резюме", "Отправлено", "Просмотрено", "Приглашение", "Оффер"):
        assert label in out
    # значения из данных
    assert "12345" in out
    assert "10" in out
    # ASCII-рамка
    assert "+" in out and "-" in out
    # конверсии видны как проценты
    assert "40.0" in out or "40%" in out


def test_format_funnel_md_is_pipe_table():
    out = format_funnel(_filled_funnel(), "md")
    assert "|" in out
    # markdown-разделитель
    assert "---" in out
    assert "12345" in out


def test_format_funnel_shows_zero_conversions_for_dead_resume():
    """Воронка с sent>0 но viewed=0: конверсии показываются как 0%, не падает."""
    out = format_funnel(_filled_funnel(), "table")
    # второе резюме 67890 — все конверсии 0
    assert "67890" in out
    assert "0%" in out  # целая конверсия → без дробной части (%g)


def test_format_funnel_unknown_format_raises():
    import pytest

    with pytest.raises(ValueError):
        format_funnel(_filled_funnel(), "csv")  # воронка — только table/md
    with pytest.raises(ValueError):
        format_funnel(_filled_funnel(), "xml")


# --- format_dead -----------------------------------------------------------


def test_format_dead_all_formats_do_not_crash():
    for fmt in ("table", "md"):
        out = format_dead({"total_sent": 0, "dead": 0, "dead_rate": 0.0}, fmt)
        assert isinstance(out, str)
        assert _has_emoji(out) is False


def test_format_dead_table_shows_counts_and_rate():
    out = format_dead({"total_sent": 10, "dead": 3, "dead_rate": 30.0}, "table")
    assert "10" in out
    assert "3" in out
    assert "30" in out  # dead_rate
    # подпись «мёртвой зоны»
    assert "мёртв" in out.lower() or "без ответ" in out.lower()


def test_format_dead_md_is_pipe_table():
    out = format_dead({"total_sent": 4, "dead": 1, "dead_rate": 25.0}, "md")
    assert "|" in out
    assert "25" in out


def test_format_dead_zero_rate_does_not_crash():
    out = format_dead({"total_sent": 0, "dead": 0, "dead_rate": 0.0}, "table")
    assert isinstance(out, str)
