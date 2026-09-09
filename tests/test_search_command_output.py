"""Тесты форматирования вывода команды search (issue #14).

Проверяем, что новое поле salary рендерится в строку карточки аккуратно:
присутствует когда есть, отсутствует (без пустых скобок) когда нет.
Без браузера — чистые функции _format_salary / _format_card_line.
"""

from __future__ import annotations

import pytest

from hhru_bot.commands.search import _format_card_line, _format_salary
from hhru_bot.search import SalaryInfo, VacancyCard

pytestmark = pytest.mark.integration


def _card(salary=None, title="Dev", company="Acme", url="https://hh.ru/vacancy/1"):
    return VacancyCard(vacancy_id="1", title=title, company=company, url=url, salary=salary)


# --- _format_salary ---


def test_format_salary_none_empty():
    assert _format_salary(None) == ""


def test_format_salary_range():
    s = SalaryInfo(150000, 200000, "RUB", "raw")
    assert _format_salary(s) == "150000-200000 RUB"


def test_format_salary_from_only():
    s = SalaryInfo(80000, None, "RUB", "raw")
    assert _format_salary(s) == "от 80000 RUB"


def test_format_salary_to_only():
    s = SalaryInfo(None, 120000, "USD", "raw")
    assert _format_salary(s) == "до 120000 USD"


def test_format_salary_no_bounds_empty():
    # Защитный случай: оба None (аномальный SalaryInfo) → пустая строка
    s = SalaryInfo(None, None, "RUB", "raw")
    assert _format_salary(s) == ""


def test_format_salary_unknown_currency_omits_none():
    s = SalaryInfo(5000, 7000, None, "5 000–7 000 XYZ на руки")
    assert _format_salary(s) == "5000-7000"


# --- _format_card_line ---


def test_card_line_without_salary_is_plain():
    line = _format_card_line(_card())
    assert line == "Dev — Acme (https://hh.ru/vacancy/1)"


def test_card_line_with_salary():
    salary = SalaryInfo(100000, 100000, "RUB", "raw")
    line = _format_card_line(_card(salary=salary))
    assert "| 100000 RUB" in line
    # Без пустых скобок
    assert " / " not in line


# --- запись собранных карточек в рынок (#66) ---------------------------------
#
# search СОБИРАЕТ карточки (VacancyCard с salary, #34), но НЕ писал их в БД —
# рынок-анализ был не из чего строить. _record_seen = побочный эффект сбора:
# пишет ВСЕ собранные карточки в vacancies_seen, не трогая отбор/скоринг/вывод.


def test_record_seen_writes_all_cards(tmp_path):
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")
    cards = [
        VacancyCard(
            vacancy_id="1",
            title="Backend",
            company="Yandex",
            url="https://hh.ru/vacancy/1",
            salary=SalaryInfo(300000, 400000, "RUB", "raw"),
            vacancy_text="Python and Docker",
            side_job=True,
            no_resume=False,
            activity="Активно отвечает",
            hh_rating="4,8",
            hrbrand_winner=True,
            metro_stations=["Таганская", "Марксистская"],
        ),
        VacancyCard(
            vacancy_id="2",
            title="DevOps",
            company="Acme",
            url="https://hh.ru/vacancy/2",
            salary=None,
            metro_stations=[],
        ),
    ]
    _record_seen(cards, "python backend", history)

    rows = history.list_vacancies_seen()
    assert len(rows) == 2
    by_id = {r["vacancy_id"]: r for r in rows}
    assert by_id["1"]["salary_from"] == 300000
    assert by_id["1"]["search_query"] == "python backend"
    assert by_id["1"]["vacancy_text"] == "Python and Docker"
    assert by_id["1"]["side_job"] == 1
    assert by_id["1"]["no_resume"] == 0
    assert by_id["1"]["activity"] == "Активно отвечает"
    assert by_id["1"]["hh_rating"] == "4,8"
    assert by_id["1"]["hrbrand_winner"] == 1
    assert by_id["1"]["metro_stations"] == '["Таганская", "Марксистская"]'
    # вакансия без зарплаты тоже записана
    assert by_id["2"]["salary_from"] is None
    assert by_id["2"]["metro_stations"] == "[]"


def test_record_seen_preserves_history_when_company_selector_misses(tmp_path):
    """Пустая company не должна превращать известного работодателя в unknown (#532)."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")
    history.upsert_vacancy_seen(
        vacancy_id="1",
        search_query="python",
        title="Backend",
        company="Yandex",
        employer_tier="top_tech",
        is_remote=True,
    )

    _record_seen(
        [
            VacancyCard(
                vacancy_id="1",
                title="",
                company="",
                url="https://hh.ru/vacancy/1",
                is_remote=None,
            )
        ],
        "python",
        history,
    )

    row = history.list_vacancies_seen()[0]
    assert row["title"] == "Backend"
    assert row["company"] == "Yandex"
    assert row["employer_tier"] == "top_tech"
    assert row["is_remote"] == 1


def test_record_seen_preserves_employer_tier_when_rating_selector_misses(tmp_path):
    """Company-имя есть, но rating/reviews-блок не отрендерился (селектор-промах,
    не «компания реально неизвестна») — ранее подтверждённый tier не должен
    затираться на "unknown" (review-финдинг PR #539: classify_employer(company,
    None) для нетоповой компании возвращает непустую строку "unknown", которая
    раньше проходила COALESCE(NULLIF(...)) как достоверное новое значение)."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")
    history.upsert_vacancy_seen(
        vacancy_id="1",
        search_query="python",
        title="Backend",
        company="ООО Ромашка",
        employer_tier="mid",
    )

    _record_seen(
        [
            VacancyCard(
                vacancy_id="1",
                title="Backend",
                company="ООО Ромашка",
                url="https://hh.ru/vacancy/1",
                employer_info=None,  # rating/reviews-блок не найден на этом scrape
            )
        ],
        "python",
        history,
    )

    row = history.list_vacancies_seen()[0]
    assert row["employer_tier"] == "mid"


def test_record_seen_preserves_tier_when_reviews_count_selector_drifts(tmp_path):
    """PR #539 (cycle 2): partial-failure path. employer_info есть (rating/trusted
    выжили), но reviews_count-селектор дрейфнул → reviews_count is None. Для
    нетоповой компании classify_employer возвращает "unknown" (mid требует
    reviews_count >= порога), и round-1 гейт (employer_info is None) её не
    ловил — employer_info-то не None. «unknown» затирал подтверждённый "mid"."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History
    from hhru_bot.scoring import EmployerInfo

    history = History(tmp_path / "h.db")
    history.upsert_vacancy_seen(
        vacancy_id="1",
        search_query="python",
        title="Backend",
        company="ООО Ромашка",
        employer_tier="mid",
    )

    _record_seen(
        [
            VacancyCard(
                vacancy_id="1",
                title="Backend",
                company="ООО Ромашка",
                url="https://hh.ru/vacancy/1",
                # rating/trusted выжили, reviews_count-селектор промахнулся
                employer_info=EmployerInfo(rating=4.5, reviews_count=None, trusted=True),
            )
        ],
        "python",
        history,
    )

    row = history.list_vacancies_seen()[0]
    assert row["employer_tier"] == "mid"


def test_record_seen_downgrades_to_unknown_when_reviews_count_genuinely_low(tmp_path):
    """PR #539 (cycle 2): контр-тест к over-suppression. reviews_count реально
    прочитан и ниже порога — это достоверное наблюдение «небольшой работодатель»,
    «unknown» обязан затереть прежний «mid» (гейт не должен перегащивать
    настоящий downgrade)."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History
    from hhru_bot.scoring import EmployerInfo

    history = History(tmp_path / "h.db")
    history.upsert_vacancy_seen(
        vacancy_id="1",
        search_query="python",
        title="Backend",
        company="ООО Ромашка",
        employer_tier="mid",
    )

    _record_seen(
        [
            VacancyCard(
                vacancy_id="1",
                title="Backend",
                company="ООО Ромашка",
                url="https://hh.ru/vacancy/1",
                # reviews_count прочитан, но мал — genuine unknown
                employer_info=EmployerInfo(rating=3.0, reviews_count=2, trusted=False),
            )
        ],
        "python",
        history,
    )

    row = history.list_vacancies_seen()[0]
    assert row["employer_tier"] == "unknown"


def test_record_seen_failure_does_not_raise(tmp_path):
    """Сбой записи НЕ должен валить поиск — рынок лишь удобство."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")

    def _boom(**_kwargs):
        raise RuntimeError("boom")

    history.upsert_vacancy_seen = _boom  # type: ignore[method-assign]
    cards = [VacancyCard(vacancy_id="1", title="T", company="C", url="https://hh.ru/vacancy/1")]
    _record_seen(cards, "python", history)  # не должно упасть


def test_search_text_overrides_resume_without_mutating_config(monkeypatch):
    import argparse

    from hhru_bot.commands.search import _resumes_for_search
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig

    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python", exclude_employers=["BadCorp"]),
    )
    config = AppConfig(
        storage_state_file=__import__("pathlib").Path("state.json"),
        throttle=ThrottleConfig(),
        cover_letter_default="hello",
        resumes=[resume],
    )
    args = argparse.Namespace(resume="python", text="Тестировщик")

    actual = _resumes_for_search(config, args)

    assert actual[0].search.text == "Тестировщик"
    assert actual[0].search.exclude_employers == ["BadCorp"]
    assert resume.search.text == "python"


def test_search_text_without_resume_uses_empty_default_filters():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search
    from hhru_bot.config import AppConfig, SearchFilters, ThrottleConfig

    config = AppConfig(
        storage_state_file=__import__("pathlib").Path("state.json"),
        throttle=ThrottleConfig(),
        cover_letter_default="hello",
        resumes=[],
    )
    args = argparse.Namespace(resume=None, text="Тестировщик")

    actual = _resumes_for_search(config, args)

    assert len(actual) == 1
    assert actual[0].search == SearchFilters(text="Тестировщик")
    assert actual[0].resume_id.startswith("adhoc-")


# --- search --area (issue-city-search-gaps, воркстрим B1) -------------------
#
# --area оверлеит id города/региона (hh.ru area, напр. 1641 = Набережные
# Челны, 88 = Казань) поверх фильтров резюме. Покрыты 4 комбинации:
# (--resume | ad-hoc) x (--text | без --text). Обратная совместимость
# Namespace без атрибута area защищена тестами выше (они не передают area).


def _search_config(resumes):
    import pathlib

    from hhru_bot.config import AppConfig, ThrottleConfig

    return AppConfig(
        storage_state_file=pathlib.Path("state.json"),
        throttle=ThrottleConfig(),
        cover_letter_default="hello",
        resumes=resumes,
    )


def _configured_resume():
    from hhru_bot.config import ResumeConfig, SearchFilters

    return ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python", exclude_employers=["BadCorp"]),
    )


def test_resume_mode_area_with_text_overlays_both():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search

    resume = _configured_resume()
    config = _search_config([resume])
    args = argparse.Namespace(resume="python", text="Тестировщик", area=1641)

    actual = _resumes_for_search(config, args)

    assert actual[0].search.text == "Тестировщик"
    assert actual[0].search.area == 1641
    assert actual[0].search.exclude_employers == ["BadCorp"]
    # иммутабельность: объект конфига не мутирован
    assert resume.search.text == "python"
    assert resume.search.area is None


def test_resume_mode_area_without_text_keeps_configured_text():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search

    resume = _configured_resume()
    config = _search_config([resume])
    args = argparse.Namespace(resume="python", text=None, area=1641)

    actual = _resumes_for_search(config, args)

    assert actual[0].search.text == "python"
    assert actual[0].search.area == 1641
    assert resume.search.area is None


def test_adhoc_text_with_area_builds_synthetic_resume():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search
    from hhru_bot.config import SearchFilters

    config = _search_config([])
    args = argparse.Namespace(resume=None, text="сборщик", area=1641)

    actual = _resumes_for_search(config, args)

    assert len(actual) == 1
    assert actual[0].search == SearchFilters(text="сборщик", area=1641)
    assert actual[0].resume_id.startswith("adhoc-")


def test_adhoc_area_reaches_search_url():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search
    from hhru_bot.search import build_search_url

    config = _search_config([])
    args = argparse.Namespace(resume=None, text="сборщик", area=1641)

    actual = _resumes_for_search(config, args)

    url = build_search_url(actual[0].search)
    assert "area=1641" in url


def test_area_only_overlays_all_configured_resumes():
    import argparse

    from hhru_bot.commands.search import _resumes_for_search

    resume = _configured_resume()
    config = _search_config([resume])
    args = argparse.Namespace(resume=None, text=None, area=1641)

    actual = _resumes_for_search(config, args)

    assert len(actual) == 1
    assert actual[0].search.text == "python"
    assert actual[0].search.area == 1641
    assert resume.search.area is None


def test_adhoc_history_isolated_by_area():
    """Один и тот же текст в разных городах — разные ad-hoc запросы: их
    локальная история (resume_id из query_key) не должна смешиваться."""
    import argparse

    from hhru_bot.commands.search import _resumes_for_search

    config = _search_config([])

    def adhoc_id(area):
        args = argparse.Namespace(resume=None, text="сборщик", area=area)
        return _resumes_for_search(config, args)[0].resume_id

    assert adhoc_id(1641) != adhoc_id(88)
    assert adhoc_id(1641) == adhoc_id(1641)
    # legacy-изоляция: запрос без area остаётся отдельным от запроса с area
    assert adhoc_id(None) != adhoc_id(1641)


def test_adhoc_query_key_does_not_collide_with_area_syntax_in_text():
    """m3: композиция key_source обязана быть инъективной.

    --text со литералом "|area=88" (без --area) не должен получать тот же
    query_key, что --text X --area 88: склейка строкой неоднозначна и слила бы
    две разные ad-hoc истории в одну.
    """
    import argparse

    from hhru_bot.commands.search import _resumes_for_search

    config = _search_config([])

    def adhoc_id(text, area):
        args = argparse.Namespace(resume=None, text=text, area=area)
        return _resumes_for_search(config, args)[0].resume_id

    assert adhoc_id("сборщик|area=88", None) != adhoc_id("сборщик", 88)


def test_register_area_rejects_non_positive_ids():
    """m4: --area 0/-1 — не id каталога hh.ru; argparse отклоняет их до run(),
    по прецеденту _positive_page_count/_nonnegative_limit (#441)."""
    import argparse

    from hhru_bot.commands import search as search_cmd

    parser = argparse.ArgumentParser()
    search_cmd.register(parser.add_subparsers())

    parsed = parser.parse_args(["search", "--area", "1641"])
    assert parsed.area == 1641

    for bad in ("0", "-1"):
        with pytest.raises(SystemExit):
            parser.parse_args(["search", "--area", bad])


# --- VacancySearchIndeterminate не должен выдаваться за успешный результат ---
#
# cycle-review PR #460 (round 1): удалённый `continue` после `failed = True`
# заставлял partial_results (недостоверный снимок) течь дальше в _record_seen
# (засоряя рынок недостоверными данными) и filter_candidates/rank_candidates
# (печатая их как подтверждённых кандидатов) вместо перехода к следующему
# резюме. Команда read-only, но вывод/рынок не должны путать partial с success.


def test_indeterminate_search_skips_resume_without_recording_partial_results(tmp_path, monkeypatch):
    import argparse

    from hhru_bot.commands import search as search_command
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.history import History
    from hhru_bot.search import VacancySearchIndeterminate

    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return object()

    partial = [
        VacancyCard("partial-0", "Python partial", "Acme", "https://hh.ru/vacancy/partial-0")
    ]
    record_seen_calls: list[list[VacancyCard]] = []

    def search(_page, _filters, max_pages):  # noqa: ARG001
        raise VacancySearchIndeterminate(
            "timeout",
            state="indeterminate",
            page_num=0,
            url="https://hh.ru/search/vacancy",
            partial_results=partial,
        )

    def record_seen(cards, _query, _history, **_kwargs):
        record_seen_calls.append(cards)

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _Context())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.search.search_vacancies", search)
    monkeypatch.setattr(search_command, "_record_seen", record_seen)

    args = argparse.Namespace(
        config=None,
        history=str(tmp_path / "history.db"),
        account=None,
        resume=None,
        max_pages=1,
        headless=True,
    )

    History(args.history)
    failed = search_command.run(args)

    assert failed is True
    # partial_results must never reach the market-recording side effect nor be
    # printed as confirmed candidates -- the resume is skipped entirely.
    assert record_seen_calls == []


# --- E4 (issue-city-search-gaps, факт 7): score UX без skonфигурированного scoring ---
#
# Без scoring-секции rank_candidates получает _ZERO_WEIGHTS и все кандидаты
# имеют score=+0.00 BY DESIGN (порядок выдачи hh.ru). Нулевой score на каждой
# строке — шум, который выглядит как поломка: печатаем один [INFO] в шапке
# резюме, а score= на строках [candidate] не выводим. При заданном scoring
# вывод со score= не меняется (обратная совместимость).


def _run_search_with_rank(monkeypatch, tmp_path, resumes, ranked):
    """Мокает браузер/поиск/отбор/ранжирование в run() до печати; возвращает args."""
    import argparse

    config = _search_config(resumes)

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return object()

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _Context())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies",
        lambda _page, _filters, max_pages: [card for card, _score, _b in ranked],
    )
    monkeypatch.setattr(search_command_module(), "_record_seen", lambda cards, _q, _h, **_k: None)
    monkeypatch.setattr(
        "hhru_bot.search.filter_candidates", lambda cards, *a, **k: (list(cards), [])
    )
    monkeypatch.setattr("hhru_bot.search.rank_candidates", lambda _c, _f, _r: list(ranked))

    return argparse.Namespace(
        config=None,
        history=str(tmp_path / "history.db"),
        account=None,
        resume=None,
        max_pages=1,
        headless=True,
    )


def search_command_module():
    from hhru_bot.commands import search as search_command

    return search_command


def test_run_without_scoring_prints_info_and_omits_per_line_score(tmp_path, monkeypatch, capsys):
    args = _run_search_with_rank(
        monkeypatch, tmp_path, [_configured_resume()], [(_card(), 0.0, {})]
    )

    failed = search_command_module().run(args)

    assert failed is False
    out = capsys.readouterr().out
    assert out.count("[INFO] scoring не сконфигурирован — порядок выдачи hh.ru") == 1
    assert "[candidate]" in out
    assert "score=" not in out


def test_run_with_scoring_keeps_score_output(tmp_path, monkeypatch, capsys):
    import dataclasses
    from types import SimpleNamespace

    resume = dataclasses.replace(_configured_resume(), scoring=SimpleNamespace())
    args = _run_search_with_rank(monkeypatch, tmp_path, [resume], [(_card(), 2.5, {"tier": 2.5})])

    failed = search_command_module().run(args)

    assert failed is False
    out = capsys.readouterr().out
    assert "score=+2.50" in out
    assert "tier=+2.50" in out
    assert "[INFO] scoring не сконфигурирован" not in out


def test_info_printed_once_per_resume_not_per_line(tmp_path, monkeypatch, capsys):
    from hhru_bot.config import ResumeConfig, SearchFilters

    second = ResumeConfig(
        id="qa", resume_url="https://hh.ru/resume/BBB222", search=SearchFilters(text="qa")
    )
    ranked = [(_card(), 0.0, {}), (_card(), 0.0, {})]
    args = _run_search_with_rank(monkeypatch, tmp_path, [_configured_resume(), second], ranked)

    failed = search_command_module().run(args)

    assert failed is False
    out = capsys.readouterr().out
    # по одному [INFO] на каждое резюме (2 кандидата на резюме — не на строку)
    assert out.count("[INFO] scoring не сконфигурирован — порядок выдачи hh.ru") == 2
    assert out.count("[candidate]") == 4
    assert "score=" not in out
