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

    def record_seen(cards, _query, _history):
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
