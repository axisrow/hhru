"""Тесты эвристической оценки ЗП для вакансий без указанной (#93, часть B).

~50% вакансий на hh.ru реально без ЗП. estimate_salary даёт оценку по медиане
salary_to собранных вакансий по (search_query, employer_tier). Гипотеза
пользователя «известные платят меньше» проверяется ДАННЫМИ: коэффициенты tier'ов
— это медианы по tier внутри сферы, а не априорные константы. Если на практике
top_tech платит меньше unknown — оценка для top_tech будет ниже автоматически.

Без браузера — только SQLite + чистая логика медианы/fallback.
"""

from __future__ import annotations

from hhru_bot.history import History


def _seen(h, vacancy_id, search_query, tier, salary_to):
    """Вставка вакансии с ЗП (salary_to) и tier для тестов estimate."""
    h.upsert_vacancy_seen(
        vacancy_id=vacancy_id,
        search_query=search_query,
        company="C",
        salary_from=salary_to,
        salary_to=salary_to,
        salary_currency="RUB",
        employer_tier=tier,
    )


# --- медиана по (query, tier) ------------------------------------------------


def test_estimate_returns_tier_median_when_enough_data(tmp_path):
    """Достаточно данных по tier (n>=5) → медиана salary_to по (query, tier)."""
    h = History(tmp_path / "h.db")
    # top_tech: 100, 200, 300, 400, 500 → медиана 300
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"t{i}", "python", "top_tech", s)

    est = h.estimate_salary("python", "top_tech")
    assert est is not None
    assert est.salary_to == 300
    assert est.salary_from == 300  # фиксированная оценка (from=to=медиана)
    assert est.currency == "RUB"


def test_estimate_falls_back_to_sphere_when_tier_too_few(tmp_path):
    """По tier < 5 вакансий → fallback на медиану по всей сфере (любой tier).

    Мало данных по tier → медиана шумная → честнее сфера целиком.
    """
    h = History(tmp_path / "h.db")
    # top_tech: только 2 значения (мало) — 1000, 2000
    _seen(h, "t0", "python", "top_tech", 1000)
    _seen(h, "t1", "python", "top_tech", 2000)
    # unknown (другой tier): 100, 200, 300, 400, 500 → медиана сферы 300
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"u{i}", "python", "unknown", s)

    est = h.estimate_salary("python", "top_tech")
    assert est is not None
    # НЕ медиана top_tech (1500), а медиана всей сферы — fallback сработал.
    # Сфера: [1000, 2000 (top_tech) + 100..500 (unknown)] = 7 значений,
    # медиана = центральное = 400.
    assert est.salary_to == 400


def test_estimate_returns_none_when_sphere_empty(tmp_path):
    """Данных по сфере нет вообще → None (оценки не существует)."""
    h = History(tmp_path / "h.db")
    assert h.estimate_salary("python", "top_tech") is None


def test_estimate_returns_none_when_no_salary_data(tmp_path):
    """В сфере есть вакансии, но все без ЗП → None."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        search_query="python",
        company="C",
        employer_tier="unknown",  # salary_to IS NULL
    )
    assert h.estimate_salary("python", "unknown") is None


# --- гипотеза «известные платят меньше» проверяется данными ------------------


def test_estimate_top_tech_can_be_lower_than_unknown_from_data(tmp_path):
    """Суть #93: коэффициенты tier'ов ИЗ ДАННЫХ. Если в собранных вакансиях
    top_tech реально платит МЕНЬШЕ unknown — estimate для top_tech будет НИЖЕ.
    Это и есть проверка гипотезы пользователя «известные платят меньше» на
    практике, а не априорная константа «top_tech × 1.5»."""
    h = History(tmp_path / "h.db")
    # top_tech: 100..500 → медиана 300 (Яндекс/Сбер, гипотеза «платят меньше»)
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"t{i}", "python", "top_tech", s)
    # unknown: 600..1000 → медиана 800 (мелкие ООО, по гипотезе платят больше)
    for i, s in enumerate([600, 700, 800, 900, 1000]):
        _seen(h, f"u{i}", "python", "unknown", s)

    top_est = h.estimate_salary("python", "top_tech")
    unk_est = h.estimate_salary("python", "unknown")
    assert top_est is not None
    assert unk_est is not None
    top = top_est.salary_to
    unk = unk_est.salary_to
    assert top is not None and unk is not None
    # Данные говорят top_tech < unknown → оценка это отражает (гипотеза верна здесь).
    assert top < unk


def test_estimate_top_tech_can_be_higher_when_data_says_so(tmp_path):
    """Обратный случай: если данные говорят top_tech > unknown — оценка выше.
    Доказывает, что коэффициенты берутся ИЗ ДАННЫХ, а не захардкожены в одну
    сторону (иначе это была бы априорная константа, что #93 запрещает)."""
    h = History(tmp_path / "h.db")
    for i, s in enumerate([600, 700, 800, 900, 1000]):
        _seen(h, f"t{i}", "python", "top_tech", s)
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"u{i}", "python", "unknown", s)

    top_est = h.estimate_salary("python", "top_tech")
    unk_est = h.estimate_salary("python", "unknown")
    assert top_est is not None
    assert unk_est is not None
    top = top_est.salary_to
    unk = unk_est.salary_to
    assert top is not None and unk is not None
    assert top > unk


# --- сепарация по сфере ------------------------------------------------------


def test_estimate_isolated_per_search_query(tmp_path):
    """Медиана считается в рамках ОДНОЙ сферы, не смешивается с другими."""
    h = History(tmp_path / "h.db")
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"p{i}", "python", "unknown", s)
    for i, s in enumerate([1000, 2000, 3000, 4000, 5000]):
        _seen(h, f"j{i}", "java", "unknown", s)

    py = h.estimate_salary("python", "unknown")
    ja = h.estimate_salary("java", "unknown")
    assert py is not None and ja is not None
    assert py.salary_to == 300
    assert ja.salary_to == 3000


# --- маркировка derived-view -------------------------------------------------


def test_estimate_raw_marks_it_as_estimated(tmp_path):
    """Оценка честно помечается в raw (derived-view, отличать от реальной ЗП)."""
    h = History(tmp_path / "h.db")
    for i, s in enumerate([100, 200, 300, 400, 500]):
        _seen(h, f"t{i}", "python", "top_tech", s)
    est = h.estimate_salary("python", "top_tech")
    assert est is not None
    assert "оценк" in est.raw.lower()
