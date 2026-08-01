"""Тесты разделения валют в агрегатах рынка (#122).

salary_currency в vacancies_seen НЕ нормализована: hh.ru отдаёт рублёвые вилки
и «от 6000 USD» в одной выдаче. Раньше медиана считалась по всем salary_to
подряд, поэтому валютная вакансия вставала в ряд как самая низкооплачиваемая и
незаметно занижала медиану сферы.

Теперь медиана считается по ДОМИНИРУЮЩЕЙ валюте сферы, остальные выносятся в
счётчик other_currency и сноску отчёта. Без браузера — только SQLite.
"""

from __future__ import annotations

from hhru_bot.history import History
from hhru_bot.report_market import market_summary


def _seen(
    h: History,
    vid: str,
    query: str,
    salary_to: int | None,
    currency: str | None,
    tier: str | None = None,
):
    h.upsert_vacancy_seen(
        vacancy_id=vid,
        title=f"Вакансия {vid}",
        company="ООО Тест",
        salary_from=None,
        salary_to=salary_to,
        salary_currency=currency,
        search_query=query,
        employer_tier=tier,
    )


class TestDominantCurrency:
    def test_usd_outlier_does_not_drag_median_down(self, tmp_path):
        """Ключевая регрессия #122: «6000 USD» не должен занижать рублёвую медиану."""
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([200000, 250000, 300000]):
            _seen(h, f"r{i}", "python", amount, "RUB")
        _seen(h, "usd1", "python", 6000, "USD")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "RUB"
        assert row["median_to"] == 250000  # медиана рублёвых, USD не участвует
        assert row["other_currency"] == 1

    def test_dominant_currency_is_the_most_frequent(self, tmp_path):
        h = History(tmp_path / "h.db")
        for i in range(3):
            _seen(h, f"u{i}", "relocate", 5000 + i * 1000, "USD")
        _seen(h, "r1", "relocate", 300000, "RUB")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "USD"
        assert row["median_to"] == 6000
        assert row["other_currency"] == 1

    def test_single_currency_has_no_other(self, tmp_path):
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([100000, 200000]):
            _seen(h, f"r{i}", "python", amount, "RUB")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "RUB"
        assert row["other_currency"] == 0

    def test_vacancies_without_salary_are_not_other_currency(self, tmp_path):
        """Вакансия без ЗП не «другая валюта» — она просто без данных."""
        h = History(tmp_path / "h.db")
        _seen(h, "r1", "python", 200000, "RUB")
        _seen(h, "n1", "python", None, None)

        row = h.market_salary_by_query()[0]
        assert row["count"] == 2
        assert row["with_salary"] == 1
        assert row["other_currency"] == 0

    def test_usd_without_salary_to_is_not_counted(self, tmp_path):
        """«от 6000 USD» (salary_to пуст) в медиану и так не шёл — не считаем его."""
        h = History(tmp_path / "h.db")
        _seen(h, "r1", "python", 200000, "RUB")
        _seen(h, "u1", "python", None, "USD")

        row = h.market_salary_by_query()[0]
        assert row["other_currency"] == 0


class TestEstimatesRespectCurrency:
    """#122 + #93: оценки ЗП не должны возвращать смешение валют назад.

    ``_augment_with_estimates`` фильтрует РЕАЛЬНЫЕ ЗП по доминирующей валюте, но
    сами оценки берутся из ``_median_salary_to``. Если у той нет фильтра валюты,
    рублёвая строка попадает в медиану, помеченную как USD, — ровно тот перекос,
    ради которого заведён #122, только через другой вход.
    """

    def test_estimate_does_not_mix_other_currency(self, tmp_path):
        """USD-сфера с одной рублёвой вилкой: оценка считается только по USD."""
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([5000, 6000, 7000]):
            _seen(h, f"u{i}", "relocate", amount, "USD", tier="mid")
        # Рублёвая вилка на три порядка выше — если просочится в оценку, медиана
        # уедет вверх и это будет видно.
        _seen(h, "r1", "relocate", 300000, "RUB", tier="mid")
        for i in range(2):
            _seen(h, f"n{i}", "relocate", None, None, tier="mid")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["currency"] == "USD"
        assert row["other_currency"] == 1
        assert row["estimated"] is True
        # Реальные USD: [5000, 6000, 7000]; оценка по USD = 6000, добавлена дважды
        # (две вакансии без ЗП) → [5000, 6000, 6000, 6000, 7000] → медиана 6000.
        # С рублёвой строкой внутри оценка была бы 6500 → медиана 6500.
        assert row["median_to"] == 6000

    def test_tier_fallback_to_sphere_also_filtered(self, tmp_path):
        """Фоллбек «нет данных по tier → медиана по сфере» тоже по валюте."""
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([5000, 6000, 7000]):
            _seen(h, f"u{i}", "relocate", amount, "USD", tier="mid")
        _seen(h, "r1", "relocate", 300000, "RUB", tier="mid")
        # tier без единой ЗП → tier-медиана пуста → фоллбек на всю сферу.
        _seen(h, "n1", "relocate", None, None, tier="top_tech")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["currency"] == "USD"
        # Реальные [5000, 6000, 7000] + оценка по сфере (USD) 6000 → медиана 6000.
        assert row["median_to"] == 6000

    def test_single_currency_estimates_unchanged(self, tmp_path):
        """Без второй валюты поведение оценок (#93) прежнее — фильтр не мешает."""
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([200000, 240000]):
            _seen(h, f"r{i}", "python", amount, "RUB", tier="mid")
        for i in range(2):
            _seen(h, f"n{i}", "python", None, None, tier="mid")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["estimated"] is True
        assert row["median_to"] == 220000


class TestSummaryRendering:
    def test_currency_column_present(self, tmp_path):
        h = History(tmp_path / "h.db")
        _seen(h, "r1", "python", 200000, "RUB")

        out = market_summary(h.market_salary_by_query())
        assert "Валюта" in out
        assert "RUB" in out

    def test_footnote_when_other_currency_present(self, tmp_path):
        h = History(tmp_path / "h.db")
        for i, amount in enumerate([200000, 250000]):
            _seen(h, f"r{i}", "python", amount, "RUB")
        _seen(h, "u1", "python", 6000, "USD")

        out = market_summary(h.market_salary_by_query())
        assert "др. вал." in out
        assert "не вошли" in out

    def test_no_footnote_for_single_currency(self, tmp_path):
        h = History(tmp_path / "h.db")
        _seen(h, "r1", "python", 200000, "RUB")

        out = market_summary(h.market_salary_by_query())
        assert "не вошли" not in out

    def test_no_emoji_in_output(self, tmp_path):
        """Правило проекта: вывод CLI — чистый ASCII/текст."""
        h = History(tmp_path / "h.db")
        _seen(h, "r1", "python", 200000, "RUB")
        _seen(h, "u1", "python", 6000, "USD")

        out = market_summary(h.market_salary_by_query())
        assert all(ord(ch) < 0x2190 for ch in out)
