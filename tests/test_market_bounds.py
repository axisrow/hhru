"""Тесты двух медиан рынка — по нижней и по верхней границе вилки (#125).

До #125 медиана считалась ТОЛЬКО по ``salary_to``, поэтому вакансии «от 350 000»
(``salary_to IS NULL``) не участвовали в расчёте вообще. На живом прогоне #67 это
до 28% потерянной выборки и смещение медианы до 20% — причём одностороннее и
разное по сферам, что ломает главное сравнение «куда ветер дует».

Решение (вариант 1 из #125): ДВЕ отдельные медианы, каждая со своим ``n``.
Смешивать ``salary_from`` и ``salary_to`` в один ряд через COALESCE некорректно —
это разные величины; достраивать середину вилки — выдумывать отсутствующие данные.

Без браузера — только SQLite + чистый форматтер.
"""

from __future__ import annotations

from hhru_bot.history import History
from hhru_bot.report_market import market_summary


def _seen(
    h: History,
    vid: str,
    query: str,
    salary_from: int | None,
    salary_to: int | None,
    currency: str | None = "RUB",
    tier: str | None = None,
):
    h.upsert_vacancy_seen(
        vacancy_id=vid,
        title=f"Вакансия {vid}",
        company="ООО Тест",
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=currency,
        raw_date=None,
        search_query=query,
        employer_tier=tier,
    )


class TestTwoMedians:
    """Контракт: market_salary_by_query отдаёт median_from/with_from рядом с
    median_to/with_to, и обе считаются независимо друг от друга."""

    def test_both_bounds_produce_two_medians(self, tmp_path):
        h = History(tmp_path / "h.db")
        # Полные вилки: from 100/200/300 → медиана 200; to 200/300/400 → 300.
        for i, (lo, hi) in enumerate([(100, 200), (200, 300), (300, 400)]):
            _seen(h, f"v{i}", "python", lo, hi)

        row = h.market_salary_by_query()[0]
        assert row["median_from"] == 200
        assert row["median_to"] == 300
        assert row["with_from"] == 3
        assert row["with_to"] == 3

    def test_from_only_vacancies_counted_in_median_from(self, tmp_path):
        """Ключевая регрессия #125: «от N» больше не выпадает из расчёта."""
        h = History(tmp_path / "h.db")
        # Только «от»: 100, 200, 300 → медиана «от» 200; медианы «до» нет.
        for i, lo in enumerate([100, 200, 300]):
            _seen(h, f"f{i}", "python", lo, None)

        row = h.market_salary_by_query()[0]
        assert row["median_from"] == 200
        assert row["with_from"] == 3
        assert row["median_to"] == 0  # нет ни одной верхней границы
        assert row["with_to"] == 0

    def test_to_only_vacancies_counted_in_median_to(self, tmp_path):
        """Зеркальный случай: только «до N» — нижней медианы нет."""
        h = History(tmp_path / "h.db")
        for i, hi in enumerate([100, 200, 300]):
            _seen(h, f"t{i}", "python", None, hi)

        row = h.market_salary_by_query()[0]
        assert row["median_to"] == 200
        assert row["with_to"] == 3
        assert row["median_from"] == 0
        assert row["with_from"] == 0

    def test_medians_have_independent_n(self, tmp_path):
        """Каждая медиана считается по своей выборке — n у них разные."""
        h = History(tmp_path / "h.db")
        # 3 вакансии «от», 1 полная вилка → n(от)=4, n(до)=1.
        for i, lo in enumerate([100, 200, 300]):
            _seen(h, f"f{i}", "python", lo, None)
        _seen(h, "both", "python", 400, 500)

        row = h.market_salary_by_query()[0]
        assert row["with_from"] == 4
        assert row["with_to"] == 1
        # from: 100, 200, 300, 400 → (200+300)/2 = 250
        assert row["median_from"] == 250
        assert row["median_to"] == 500

    def test_bounds_are_not_mixed_into_one_series(self, tmp_path):
        """Явный анти-COALESCE-тест: «от 300» и «до 300» не встают в один ряд.

        Смешанный ряд COALESCE(salary_to, salary_from) = [100, 300] дал бы медиану
        200 в одной колонке. Раздельно: от = 300 (одна вакансия), до = 100.
        """
        h = History(tmp_path / "h.db")
        _seen(h, "f1", "python", 300, None)  # «от 300»
        _seen(h, "t1", "python", None, 100)  # «до 100»

        row = h.market_salary_by_query()[0]
        assert row["median_from"] == 300
        assert row["median_to"] == 100

    def test_single_vacancy_full_range(self, tmp_path):
        h = History(tmp_path / "h.db")
        _seen(h, "v1", "python", 150, 250)

        row = h.market_salary_by_query()[0]
        assert row["median_from"] == 150
        assert row["median_to"] == 250
        assert row["with_from"] == 1
        assert row["with_to"] == 1
        assert row["count"] == 1

    def test_no_salary_at_all(self, tmp_path):
        """Сфера без единой ЗП: обе медианы 0, count считает все вакансии."""
        h = History(tmp_path / "h.db")
        for i in range(3):
            _seen(h, f"n{i}", "python", None, None, currency=None)

        row = h.market_salary_by_query()[0]
        assert row["median_from"] == 0
        assert row["median_to"] == 0
        assert row["with_from"] == 0
        assert row["with_to"] == 0
        assert row["count"] == 3

    def test_empty_db(self, tmp_path):
        h = History(tmp_path / "h.db")
        assert h.market_salary_by_query() == []

    def test_with_salary_counts_any_bound(self, tmp_path):
        """``with_salary`` — покрытие «хоть какая-то ЗП указана».

        До #125 это был COUNT(salary_to), т.е. вакансия «от 350 000» считалась
        как «без ЗП». Теперь она в покрытие входит — иначе отчёт занижает
        доверие к медиане «от», которую сам же и показывает.
        """
        h = History(tmp_path / "h.db")
        _seen(h, "f1", "python", 300, None)
        _seen(h, "t1", "python", None, 200)
        _seen(h, "b1", "python", 100, 150)
        _seen(h, "n1", "python", None, None, currency=None)

        row = h.market_salary_by_query()[0]
        assert row["count"] == 4
        assert row["with_salary"] == 3  # три вакансии хоть с одной границей


class TestCurrencyAppliesToBothMedians:
    """#122 не должен сломаться: медианы считаются по доминирующей валюте сферы,
    и фильтр обязан применяться к ОБЕИМ, иначе смешение вернётся через «от»."""

    def test_usd_from_outlier_does_not_drag_median_from(self, tmp_path):
        h = History(tmp_path / "h.db")
        for i, lo in enumerate([200000, 250000, 300000]):
            _seen(h, f"r{i}", "python", lo, None, currency="RUB")
        _seen(h, "u1", "python", 6000, None, currency="USD")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "RUB"
        assert row["median_from"] == 250000  # USD не участвует
        assert row["with_from"] == 3

    def test_dominant_currency_shared_by_both_medians(self, tmp_path):
        """Одна валюта на сферу — обе медианы в ней, иначе колонка «Валюта» врёт.

        Рублёвые вакансии — только «от», долларовые — только «до». Если бы
        доминирующая валюта считалась по каждой границе отдельно, медиана «от»
        оказалась бы в RUB, а «до» — в USD, при единственной колонке валюты.
        """
        h = History(tmp_path / "h.db")
        for i, lo in enumerate([200000, 250000, 300000]):
            _seen(h, f"r{i}", "python", lo, None, currency="RUB")
        _seen(h, "u1", "python", None, 6000, currency="USD")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "RUB"  # 3 RUB против 1 USD
        assert row["median_from"] == 250000
        # USD-вакансия в медиану «до» не идёт — валюта не доминирующая.
        assert row["median_to"] == 0
        assert row["with_to"] == 0
        assert row["other_currency"] == 1

    def test_other_currency_counts_any_bound(self, tmp_path):
        """«от 6000 USD» теперь тоже вне медианы по валюте — значит, считается
        в other_currency (до #125 у него не было salary_to и он молча терялся)."""
        h = History(tmp_path / "h.db")
        for i, lo in enumerate([200000, 250000]):
            _seen(h, f"r{i}", "python", lo, lo + 50000, currency="RUB")
        _seen(h, "u1", "python", 6000, None, currency="USD")

        row = h.market_salary_by_query()[0]
        assert row["currency"] == "RUB"
        assert row["other_currency"] == 1


class TestSortingWithSmallSample:
    """Дефект читаемости из прогона #67: строка на n=2 встала НАВЕРХУ таблицы
    как «лидер рынка». Сортировка обязана учитывать размер выборки."""

    def test_small_sample_does_not_outrank_reliable_sphere(self, tmp_path):
        h = History(tmp_path / "h.db")
        # Ненадёжная сфера: 2 вакансии с очень высокой ЗП.
        for i, hi in enumerate([300000, 300000]):
            _seen(h, f"s{i}", "small", hi - 100000, hi)
        # Надёжная: 10 вакансий, медиана ниже.
        for i in range(10):
            _seen(h, f"b{i}", "big", 100000, 200000)

        rows = h.market_salary_by_query()
        assert rows[0]["search_query"] == "big"
        assert rows[1]["search_query"] == "small"

    def test_reliable_spheres_still_sorted_by_median_desc(self, tmp_path):
        """Среди сфер с достаточной выборкой порядок прежний — по медиане вниз
        (цель #66: выгодные направления наверху)."""
        h = History(tmp_path / "h.db")
        for i in range(10):
            _seen(h, f"h{i}", "high", 200000, 400000)
        for i in range(10):
            _seen(h, f"l{i}", "low", 100000, 200000)

        rows = h.market_salary_by_query()
        assert rows[0]["search_query"] == "high"
        assert rows[1]["search_query"] == "low"

    def test_small_samples_ordered_among_themselves(self, tmp_path):
        """Ненадёжные сферы уходят вниз, но между собой всё равно по медиане."""
        h = History(tmp_path / "h.db")
        _seen(h, "a1", "small-low", 50000, 100000)
        _seen(h, "b1", "small-high", 200000, 400000)

        rows = h.market_salary_by_query()
        assert [r["search_query"] for r in rows] == ["small-high", "small-low"]

    def test_sphere_without_upper_bound_ranked_by_lower(self, tmp_path):
        """Сфера, где ВСЕ вакансии «от N», не должна падать в конец списка.

        Сортировка только по median_to отправляла бы её вниз (верхней медианы
        нет → 0), даже если её нижние границы выше чужих верхних. Это инверсия
        ровно того сравнения, ради которого #66 считается: «от 400 000» — более
        выгодная сфера, чем «до 200 000», а не менее.
        """
        h = History(tmp_path / "h.db")
        for i, lo in enumerate([400000, 410000, 420000, 430000, 440000]):
            _seen(h, f"a{i}", "rich-from-only", lo, None)
        for i in range(6):
            _seen(h, f"b{i}", "normal", 100000, 200000)

        rows = h.market_salary_by_query()
        assert rows[0]["search_query"] == "rich-from-only"
        assert rows[0]["median_from"] == 420000
        assert rows[0]["median_to"] == 0

    def test_upper_median_still_preferred_when_both_present(self, tmp_path):
        """Когда верхняя медиана есть у обеих сфер — ранжирует именно она
        (прежнее поведение #66: потолок предложения)."""
        h = History(tmp_path / "h.db")
        # Нижняя граница выше, но потолок ниже — решает потолок.
        for i in range(6):
            _seen(h, f"a{i}", "flat", 190000, 200000)
        for i in range(6):
            _seen(h, f"b{i}", "wide", 100000, 400000)

        rows = h.market_salary_by_query()
        assert rows[0]["search_query"] == "wide"

    def test_low_sample_threshold_is_exactly_five(self, tmp_path):
        """Порог прибит тестом: n=4 ненадёжна, n=5 надёжна.

        Без этого теста _LOW_SAMPLE_N можно поменять на 3 или 7, и ничего не
        покраснеет — а порог определяет, какие строки уезжают вниз таблицы.
        """
        h = History(tmp_path / "h.db")
        for i in range(4):
            _seen(h, f"a{i}", "four", 100000, 200000)
        for i in range(5):
            _seen(h, f"b{i}", "five", 100000, 200000)

        by_q = {r["search_query"]: r for r in h.market_salary_by_query()}
        assert by_q["four"]["low_sample"] is True
        assert by_q["five"]["low_sample"] is False

    def test_equal_medians_broken_by_vacancy_count(self, tmp_path):
        """При равных медианах выше та сфера, где вакансий больше — на равных
        цифрах доверия больше к более широкой выборке."""
        h = History(tmp_path / "h.db")
        for i in range(6):
            _seen(h, f"s{i}", "smaller", 100000, 200000)
        for i in range(9):
            _seen(h, f"b{i}", "bigger", 100000, 200000)

        rows = h.market_salary_by_query()
        assert [r["search_query"] for r in rows] == ["bigger", "smaller"]

    def test_row_exposes_reliability_flag(self, tmp_path):
        """Отчёту нужен явный признак малой выборки, а не догадка по n."""
        h = History(tmp_path / "h.db")
        for i in range(10):
            _seen(h, f"b{i}", "big", 100000, 200000)
        _seen(h, "s1", "small", 100000, 200000)

        by_q = {r["search_query"]: r for r in h.market_salary_by_query()}
        assert by_q["big"]["low_sample"] is False
        assert by_q["small"]["low_sample"] is True


class TestSummaryRendersBothMedians:
    def test_table_shows_both_medians_with_their_n(self):
        """n должно быть видно РЯДОМ с каждой цифрой — медианы считаются по
        разным выборкам, и общий «С ЗП» этого не передаёт."""
        rows = [
            {
                "search_query": "python",
                "median_from": 150000,
                "median_to": 250000,
                "with_from": 33,
                "with_to": 31,
                "count": 133,
                "with_salary": 43,
                "currency": "RUB",
                "other_currency": 0,
                "estimated": False,
                "low_sample": False,
            }
        ]
        out = market_summary(rows)
        assert "150 000" in out
        assert "250 000" in out
        assert "33" in out  # n нижней медианы
        assert "31" in out  # n верхней медианы

    def test_missing_median_rendered_as_dash(self):
        rows = [
            {
                "search_query": "python",
                "median_from": 0,
                "median_to": 200000,
                "with_from": 0,
                "with_to": 5,
                "count": 5,
                "with_salary": 5,
                "currency": "RUB",
                "other_currency": 0,
                "estimated": False,
                "low_sample": False,
            }
        ]
        out = market_summary(rows)
        assert "—" in out
        assert "200 000" in out

    def test_low_sample_is_marked(self):
        """Сфера на малой выборке помечена — читатель не примет её за лидера."""
        rows = [
            {
                "search_query": "python",
                "median_from": 100000,
                "median_to": 300000,
                "with_from": 2,
                "with_to": 2,
                "count": 2,
                "with_salary": 2,
                "currency": "RUB",
                "other_currency": 0,
                "estimated": False,
                "low_sample": True,
            }
        ]
        out = market_summary(rows)
        assert "!" in out  # ASCII-маркер малой выборки
        assert "мало данных" in out.lower()

    def test_no_emoji_with_both_medians(self):
        rows = [
            {
                "search_query": "python",
                "median_from": 150000,
                "median_to": 250000,
                "with_from": 33,
                "with_to": 31,
                "count": 133,
                "with_salary": 43,
                "currency": "RUB",
                "other_currency": 2,
                "estimated": True,
                "low_sample": True,
            }
        ]
        out = market_summary(rows)
        assert all(ord(ch) < 0x2190 for ch in out)


class TestEstimatesDocumentedAsUpperBound:
    """#93: эвристические оценки строятся на ``salary_to`` и наследуют перекос.

    Решение: оценки остаются оценкой ВЕРХНЕЙ границы и достраивают только
    ``median_to``. Медиана «от» реальная — в неё оценки не подмешиваются, иначе
    верхняя граница выдавалась бы за нижнюю (то самое смешение шкал из #125).
    """

    def test_estimates_augment_only_upper_median(self, tmp_path):
        h = History(tmp_path / "h.db")
        for i, hi in enumerate([100, 200, 300, 400, 500]):
            _seen(h, f"t{i}", "python", 50, hi, tier="top_tech")
        for i in range(3):
            _seen(h, f"u{i}", "python", None, None, currency=None, tier="top_tech")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["estimated"] is True
        assert row["median_to"] == 300  # 5 реальных + 3 оценки по 300
        # Медиана «от» — только реальные пять значений по 50, без оценок.
        assert row["median_from"] == 50
        assert row["with_from"] == 5

    def test_from_only_vacancy_gets_no_invented_upper_bound(self, tmp_path):
        """Вакансия «от N» — это ДАННЫЕ, а не пропуск: достраивать ей верхнюю
        границу нельзя.

        ``_augment_with_estimates`` отбирал кандидатов по ``salary_to IS NULL``,
        поэтому реальная вакансия «от 900 000» считалась «без ЗП» и получала
        выдуманный потолок. Это ровно то достраивание вилки, которое запрещено
        дизайн-решением #125 — и оно порождало бессмыслицу, где нижняя медиана
        коридора оказывалась ВЫШЕ верхней.
        """
        h = History(tmp_path / "h.db")
        for i, hi in enumerate([100000, 200000, 300000, 400000, 500000]):
            _seen(h, f"t{i}", "python", 50000, hi, tier="mid")
        for i in range(5):
            _seen(h, f"f{i}", "python", 900000, None, tier="mid")

        row = h.market_salary_by_query(include_estimates=True)[0]
        # Оценивать нечего: вакансий вообще без ЗП нет, «от 900 000» — это данные.
        assert row["estimated"] is False
        # Верхняя медиана — по пяти РЕАЛЬНЫМ потолкам, без пяти выдуманных.
        assert row["with_to"] == 5
        assert row["median_to"] == 300000
        # Нижняя медиана — по всем десяти реальным нижним границам.
        assert row["with_from"] == 10
        assert row["median_from"] == 475000

    def test_estimates_respect_dominant_currency_of_no_salary_rows(self, tmp_path):
        """#122 на пути оценок: вакансия в ДРУГОЙ валюте не может вернуться в
        медиану через оценку.

        Она исключена из медианы и учтена в other_currency — отчёт печатает
        «не вошли в медиану». Если оценка её всё же добавит, сноска станет ложью.
        """
        h = History(tmp_path / "h.db")
        for i, hi in enumerate([100000, 200000, 300000, 400000, 500000]):
            _seen(h, f"r{i}", "python", 50000, hi, currency="RUB", tier="mid")
        # Валютная вакансия без верхней границы — вне рублёвой медианы.
        _seen(h, "u1", "python", 6000, None, currency="USD", tier="mid")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["currency"] == "RUB"
        assert row["other_currency"] == 1
        # USD-вакансия не участвует ни как значение, ни как повод для оценки.
        assert row["estimated"] is False
        assert row["median_to"] == 300000

    def test_estimates_do_not_break_from_only_sphere(self, tmp_path):
        """Сфера только с «от»: оценивать верх не из чего — median_to остаётся
        пустым, median_from не портится."""
        h = History(tmp_path / "h.db")
        for i, lo in enumerate([100, 200, 300]):
            _seen(h, f"f{i}", "python", lo, None, tier="mid")
        _seen(h, "n1", "python", None, None, currency=None, tier="mid")

        row = h.market_salary_by_query(include_estimates=True)[0]
        assert row["median_from"] == 200
        assert row["median_to"] == 0
