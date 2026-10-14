"""#1255: ширины колонок ASCII-таблиц считаются по display-width, а не len().

Эмодзи в данных hh.ru (заголовки вакансий, вопросы анкет, работодатели)
рендерятся в 2 колонки терминала при len()==1 — рамка +---+ разъезжалась.
"""

import pytest

from hhru_bot.report import _ascii_table, display_width, pad

pytestmark = pytest.mark.unit


def _lines_aligned(table: str) -> None:
    """Каждая строка таблицы занимает ровно столько же колонок, сколько рамка."""
    lines = table.splitlines()
    assert len(lines) > 2
    border_len = len(lines[0])
    assert set(lines[0]) <= {"+", "-"}
    for line in lines:
        assert display_width(line) == border_len, f"не выровнена: {line!r}"


def test_display_width():
    assert display_width("abc") == 3
    # 🚀 — East Asian Wide: рендерится в 2 колонки при len()==1.
    assert display_width("Ромашка🚀") == len("Ромашка") + 2
    # ё в виде е+U+0308: len()==2, рендерится в 1 колонку.
    assert display_width("е́") == 1
    # Флаг — два региональных индикатора, 2 колонки.
    assert display_width("🇬🇧") == 2


def test_pad_by_display_width():
    assert pad("🚀", 4) == "🚀  "
    assert pad("е́", 3) == "е́  "


def test_ascii_table_emoji_and_long_row_aligned():
    header = ["Время", "Резюме", "Действие", "Причина"]
    rows = [
        ["2026-10-13T18:00:00", "fedcba...210", "apply", "Работодатель «Ромашка🚀» в стоп-листе"],
        ["2026-10-13T18:01:00", "fedcba...210", "apply", "Ваш уровень английского языка? 🇬🇧"],
        ["2026-10-13T18:02:00", "fedcba...210", "bump", "длинная обычная причина без юникода " * 3],
        ["2026-10-13T18:03:00", "fedcba...210", "bump", "ё-короткая: е+U+0308 уже подтверждена"],
    ]
    table = _ascii_table(header, rows, footer=["Итого", "", "", "4"])
    _lines_aligned(table)


def test_ascii_table_emoji_widens_column():
    # Колонка шире, чем len() самой широкой ячейки: 🚀 считается за две.
    table = _ascii_table(["A"], [["🚀"]])
    border = table.splitlines()[0]
    assert border == "+----+"
    _lines_aligned(table)
