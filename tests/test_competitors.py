from __future__ import annotations

import pytest

from hhru_bot.commands.competitors import _collection_status, _page_cap_reached
from hhru_bot.competitors import (
    CompetitorResumeIndeterminate,
    CompetitorSearchCoverage,
    CompetitorSearchIndeterminate,
    available_search_page_count,
    build_competitor_search_url,
    coverage_warning,
    has_next_search_page,
    parse_competitor_resume_text,
    parse_search_links,
    parse_search_result_count,
    redact_free_text,
    report_competitors,
)
from hhru_bot.selector_groups import competitor_resume as selectors

pytestmark = pytest.mark.unit


DETAIL = """
Был сегодня
Мужчина
Активно ищет работу
Москва, готов работать удалённо
AI Engineer / AI Infrastructure Engineer
200 000 ₽ на руки
Специализации:
— Программист, разработчик
— Системный инженер
Тип занятости: полная занятость, проектная работа/разовое задание
Формат работы: удалённо, гибрид
Опыт работы 5 лет 3 месяца
Навыки
Уровни владения навыками
Продвинутый уровень
Python
Docker
Средний уровень
RAG
Базовый уровень
FastAPI
Образование
Высшее образование
Знание языков
Русский — Родной
Английский — B2 — Средне-продвинутый
Гражданство, время в пути до работы
Желательное время в пути до работы: Не имеет значения
"""


def test_search_url_is_keyword_only_and_page_numbered():
    url = build_competitor_search_url("AI Engineer", 2)
    assert "text=AI+Engineer" in url
    assert "page=2" in url
    assert "items_on_page=100" in url
    assert "resume=" not in url

    smoke_url = build_competitor_search_url("AI Engineer", 0, items_per_page=20)
    assert "items_on_page=20" in smoke_url


def test_search_result_count_parses_thin_space_and_coverage_warning():
    text = "Показали 12 368 резюме — остальные можно увидеть после регистрации работодателя"
    assert parse_search_result_count(text) == 12_368
    warning = coverage_warning(
        CompetitorSearchCoverage(
            total_results=12_368,
            available_pages=50,
            employer_registration_required=True,
        )
    )
    assert warning is not None
    assert "5000" in warning
    assert "50 стр. x 100" in warning
    assert "после регистрации работодателя" in warning


def test_coverage_warning_is_absent_when_all_reported_results_fit():
    coverage = CompetitorSearchCoverage(
        total_results=4_999,
        available_pages=50,
        employer_registration_required=False,
    )
    assert coverage_warning(coverage) is None


def test_coverage_uses_observed_twenty_cards_not_requested_hundred():
    warning = coverage_warning(
        CompetitorSearchCoverage(
            total_results=12_368,
            available_pages=50,
            employer_registration_required=False,
            observed_page_size=20,
        )
    )
    assert warning is not None
    assert "запрошено items_on_page=100" in warning
    assert "фактически hh.ru вернул 20" in warning
    assert "1000" in warning
    assert "50 стр. x 20" in warning


def test_parse_search_links_normalizes_url_deduplicates_and_keeps_rank():
    rows = [
        ("/resume/abc?query=AI", " AI Engineer "),
        ("/resume/abc?other=1", "duplicate"),
        ("/vacancy/123", "not a resume"),
        ("https://hh.ru/resume/def", "AI Creator"),
    ]
    cards = parse_search_links(rows, rank_offset=20)
    assert [(card.resume_id, card.desired_role, card.rank) for card in cards] == [
        ("abc", "AI Engineer", 21),
        ("def", "AI Creator", 22),
    ]
    assert cards[0].resume_url == "https://hh.ru/resume/abc"


def test_parse_detail_extracts_only_competitor_fields():
    snapshot = parse_competitor_resume_text(
        DETAIL,
        resume_id="abc",
        resume_url="https://hh.ru/resume/abc",
        headings=[
            "AI Engineer / AI Infrastructure Engineer",
            "200 000 ₽ на руки",
            "Опыт работы 5 лет 3 месяца",
            "Навыки",
            "Образование",
            "Знание языков",
        ],
    )
    assert snapshot.desired_role == "AI Engineer / AI Infrastructure Engineer"
    assert snapshot.salary_from == 200_000
    assert snapshot.salary_to == 200_000
    assert snapshot.salary_currency == "RUB"
    assert snapshot.experience_months == 63
    assert snapshot.specializations == ["Программист, разработчик", "Системный инженер"]
    assert snapshot.employment_types == [
        "полная занятость",
        "проектная работа/разовое задание",
    ]
    assert [(skill.name, skill.proficiency) for skill in snapshot.skills] == [
        ("Python", "Продвинутый уровень"),
        ("Docker", "Продвинутый уровень"),
        ("RAG", "Средний уровень"),
        ("FastAPI", "Базовый уровень"),
    ]
    # Header demographics and location are never fields on the DTO.
    assert "Москва" not in snapshot.content_hash()


def test_detail_without_confirmed_role_fails_closed():
    with pytest.raises(CompetitorResumeIndeterminate, match="desired_role"):
        parse_competitor_resume_text(
            "Навыки\nPython",
            resume_id="abc",
            resume_url="https://hh.ru/resume/abc",
            headings=["Навыки"],
        )


def test_skill_values_are_persisted_without_privacy_filter():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\nНавыки\nPython\ntest@example.com\n+7 999 123-45-67\nhttps://example.com",
        resume_id="skill-contact",
        resume_url="https://hh.ru/resume/skill-contact",
        headings=["AI Engineer", "Навыки"],
    )
    assert [skill.name for skill in snapshot.skills] == [
        "Python",
        "test@example.com",
        "+7 999 123-45-67",
        "https://example.com",
    ]


def test_numeric_role_title_is_not_misparsed_as_salary():
    snapshot = parse_competitor_resume_text(
        "3D Generalist - AI Generalist\nОпыт работы 4 года\nНавыки\nCinema 4D",
        resume_id="3d",
        resume_url="https://hh.ru/resume/3d",
        headings=["3D Generalist - AI Generalist", "Опыт работы 4 года", "Навыки"],
    )
    assert snapshot.desired_role == "3D Generalist - AI Generalist"
    assert snapshot.salary_from is None


def test_thin_space_salary_and_dashless_specialization_are_normalized():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\n2\u2009500\u00a0€ на\u00a0руки\nСпециализации:\nРазработчик\n"
        "Тип занятости: полная занятость\nОпыт работы 1\u00a0год",
        resume_id="thin",
        resume_url="https://hh.ru/resume/thin",
        headings=["AI Engineer", "2\u2009500\u00a0€ на\u00a0руки", "Опыт работы 1\u00a0год"],
    )
    assert snapshot.salary_from == 2500
    assert snapshot.salary_currency == "EUR"
    assert snapshot.specializations == ["Разработчик"]


@pytest.mark.parametrize(
    "raw",
    [
        "Пишите test@example.com или +7 999 123-45-67",
        "Меня зовут Иван",
        "Связаться с Иван Петров",
        "Живу в Москве, мне 35 лет",
        "Построил RAG-поиск и сократил latency",
    ],
)
def test_free_text_is_preserved(raw):
    assert redact_free_text(raw) == raw


def test_parse_detail_preserves_free_text_sections():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\nОбо мне\nЖиву в Москве, мне 35 лет\n"
        "Ключевые достижения\nСократил расходы на 20%",
        resume_id="free-text",
        resume_url="https://hh.ru/resume/free-text",
        headings=["AI Engineer"],
    )
    assert snapshot.experience_summary == "Живу в Москве, мне 35 лет"
    assert snapshot.achievements == "Сократил расходы на 20%"


class _Locator:
    def __init__(self, values, delayed=None, *, links=False):
        self.values = list(values)
        self.delayed = list(delayed or [])
        self.links = links
        self.wait_calls = []

    def count(self):
        return len(self.values)

    @property
    def first(self):
        return self

    def wait_for(self, *, state, timeout):
        self.wait_calls.append((state, timeout))
        self.values.extend(self.delayed)

    def nth(self, index):
        return _Text(self.values[index], links=self.links)

    def inner_text(self):
        return self.values[0]

    def get_attribute(self, name):
        assert self.links and name == "href"
        return self.values[0]


class _Text:
    def __init__(self, value, *, links=False):
        self.value = value
        self.links = links

    def inner_text(self):
        return self.value

    def get_attribute(self, name):
        assert self.links and name == "href"
        return self.value


class _PaginationPage:
    def __init__(self, pages, delayed_pages=None, delayed_block=None, next_links=None):
        self.next = _Locator(next_links or [], links=True)
        self.block = _Locator(["block"] if delayed_block is None else [], delayed_block)
        self.pages = _Locator(pages, delayed_pages)
        self.links = _Locator([])
        self.marker = self.block

    def locator(self, selector):
        if selector in (
            f"{selectors.PAGINATION_BLOCK}, {selectors.PAGINATION_LINK}",
            f"{selectors.PAGINATION_PAGE}, {selectors.PAGINATION_LINK}",
        ):
            return (
                self.marker
                if selector == f"{selectors.PAGINATION_BLOCK}, {selectors.PAGINATION_LINK}"
                else self.pages
            )
        return {
            selectors.PAGINATION_NEXT: self.next,
            selectors.PAGINATION_BLOCK: self.block,
            selectors.PAGINATION_PAGE: self.pages,
            selectors.PAGINATION_LINK: self.links,
        }[selector]


def test_pagination_waits_for_delayed_page_markers():
    page = _PaginationPage([], delayed_pages=["1", "2"])
    assert has_next_search_page(page, 0) is True
    assert page.pages.wait_calls == [("attached", 30_000)]


def test_pagination_timeout_is_indeterminate_not_last_page():
    page = _PaginationPage([])
    with pytest.raises(CompetitorSearchIndeterminate, match="не подтверждена"):
        has_next_search_page(page, 0)


def test_pagination_waits_for_delayed_container_before_declaring_last_page():
    page = _PaginationPage([], delayed_pages=["1", "2"], delayed_block=["block"])
    assert has_next_search_page(page, 0) is True
    assert page.block.wait_calls == [("attached", 30_000)]


def test_available_page_count_uses_last_visible_page():
    page = _PaginationPage(["1", "2", "50"])
    assert available_search_page_count(page, 0) == 50


def test_pagination_uses_next_link_target_instead_of_control_presence():
    assert has_next_search_page(_PaginationPage(["1"], next_links=["?page=1"]), 0) is True
    assert has_next_search_page(_PaginationPage(["1"], next_links=[""]), 0) is False
    assert has_next_search_page(_PaginationPage(["49", "50"], next_links=["?page=49"]), 49) is False


def test_page_cap_is_optional_and_only_limits_when_more_pages_exist():
    assert _page_cap_reached(None, 50, True) is False
    assert _page_cap_reached(5, 5, True) is True
    assert _page_cap_reached(5, 5, False) is False
    assert _collection_status(details_failed=1, limited=True) == "limited"


def test_report_is_deterministic_and_warns_about_limited_coverage():
    rows = [
        {
            "desired_role": "AI Engineer",
            "specializations": ["Разработчик"],
            "skills": [{"name": "Python"}, {"name": "RAG"}],
            "experience_months": 60,
            "salary_to": 200_000,
            "salary_currency": "RUB",
        },
        {
            "desired_role": "AI Engineer",
            "specializations": ["Разработчик"],
            "skills": [{"name": "Python"}],
            "experience_months": 24,
            "salary_to": 100_000,
            "salary_currency": "RUB",
        },
    ]
    report = report_competitors(rows, top=10, limited_runs=1)
    assert "Резюме в выборке: 2" in report
    assert "ограниченных запусков" in report
    assert "2  AI Engineer" in report
    assert "2  Python" in report
    assert "1  Python + RAG" in report
    assert "Медианный опыт: 42 мес." in report
    assert "RUB: 150000 (n=2)" in report
    assert "Добавляйте навык только если он подтверждён" in report
