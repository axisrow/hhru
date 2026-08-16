"""Тесты форматтера market_summary — сравнение сфер по медианной ЗП (#66).

market_summary принимает строки из History.market_salary_by_query и рендерит
ASCII-таблицу через переиспользуемый report._ascii_table (НЕ дублирует
форматтер). Только текст/ASCII — НИКАКИХ эмодзи (правило проекта).
"""

from __future__ import annotations

import pytest

from hhru_bot.report_market import market_summary

pytestmark = pytest.mark.unit

_NO_EMOJI = set(chr(c) for c in range(0x1F000, 0x1FAFF + 1)) | set(
    chr(c) for c in range(0x2600, 0x27BF + 1)
)


def _has_emoji(text: str) -> bool:
    return any(ch in _NO_EMOJI for ch in text)


def test_market_summary_renders_table_with_rows():
    rows = [
        {
            "search_query": "python",
            "median_from": 200000,
            "median_to": 300000,
            "with_from": 10,
            "with_to": 10,
            "count": 12,
            "with_salary": 10,
        },
        {
            "search_query": "performance",
            "median_from": 100000,
            "median_to": 150000,
            "with_from": 6,
            "with_to": 6,
            "count": 8,
            "with_salary": 6,
        },
    ]
    out = market_summary(rows)
    # выгодная сфера наверху (уже отсортировано history, summary не пересортирует)
    assert "python" in out
    assert "performance" in out
    # медиана форматирована читаемо (тыс. руб.), не сырой 300000
    assert "300 000" in out or "300000" in out
    # шапка колонок
    assert "Сфера" in out
    assert "Медиана" in out


def test_market_summary_empty_rows_returns_no_data():
    out = market_summary([])
    assert "Нет данных" in out or "нет данных" in out
    assert not _has_emoji(out)


def test_market_summary_has_no_emoji():
    rows = [
        {"search_query": "python", "median_to": 250000, "count": 5, "with_salary": 5},
    ]
    assert not _has_emoji(market_summary(rows))


def test_market_summary_handles_zero_median():
    """Сфера без зарплат (медиана 0) — показываем «—», не вводим в заблуждение
    нулём как реальной оценкой дохода."""
    rows = [
        {"search_query": "no-salary-sphere", "median_to": 0, "count": 4, "with_salary": 0},
    ]
    out = market_summary(rows)
    assert "—" in out
    assert "0" not in out.split("Медиана")[-1].split("\n")[0] if "Медиана" in out else True


def test_market_summary_shows_coverage():
    """Доля вакансий с зарплатой — для понимания, насколько медиана доверительна
    (мало данных с ЗП → оценка шаткая)."""
    rows = [
        {
            "search_query": "python",
            "median_from": 200000,
            "median_to": 300000,
            "with_from": 10,
            "with_to": 10,
            "count": 12,
            "with_salary": 10,
        },
    ]
    out = market_summary(rows)
    # with_salary/count видны — coverage сигнала. Ищем колонки строки целиком,
    # а не подстроки «10»/«12»: после #125 в ячейках медиан есть свои n, и
    # голая подстрока прошла бы и без колонок покрытия.
    cells = [c.strip() for c in out.splitlines()[3].strip("|").split("|")]
    assert cells[-2] == "12"  # Вакансий
    assert cells[-1] == "10"  # С ЗП


# --- #93: пометка ~оценка для эвристических ЗП --------------------------------


def test_market_summary_marks_estimated_sphere():
    """#93: сфера, медиана которой построена с оценками, помечается ~ (ASCII-тильда,
    НЕ эмодзи) — честно отличать реальную медиану от derived-view оценки."""
    rows = [
        {
            "search_query": "python",
            "median_from": 0,
            "median_to": 300000,
            "with_from": 0,
            "with_to": 2,
            "count": 12,
            "with_salary": 2,
            "estimated": True,
        },
    ]
    out = market_summary(rows)
    assert "~300 000" in out or "~300000" in out
    assert not _has_emoji(out)


def test_market_summary_does_not_mark_real_salary():
    """#93: сфера с реальной медианой (estimated=False/отсутствует) — БЕЗ ~."""
    rows = [
        {
            "search_query": "python",
            "median_from": 200000,
            "median_to": 300000,
            "with_from": 12,
            "with_to": 12,
            "count": 12,
            "with_salary": 12,
            "estimated": False,
        },
    ]
    out = market_summary(rows)
    assert "~" not in out


def test_market_summary_estimated_no_emoji():
    """#93: пометка оценки — ASCII-тильда, в выводе нет эмодзи (правило проекта)."""
    rows = [
        {
            "search_query": "python",
            "median_to": 250000,
            "count": 5,
            "with_salary": 1,
            "estimated": True,
        },
    ]
    assert not _has_emoji(market_summary(rows))
