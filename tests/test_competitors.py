from __future__ import annotations

import pytest

import hhru_bot.competitors as competitors_module
from hhru_bot.commands.competitors import _collection_status, _page_cap_reached
from hhru_bot.competitors import (
    CompetitorResumeIndeterminate,
    CompetitorSearchCard,
    CompetitorSearchCoverage,
    CompetitorSearchIndeterminate,
    _months,
    available_search_page_count,
    build_competitor_search_url,
    coverage_warning,
    fetch_competitor_resume,
    has_next_search_page,
    parse_competitor_resume_text,
    parse_detail_business_trips_and_metro,
    parse_search_area_and_business_trips,
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


# Резюме, заполненное в английской версии hh.ru. Подписи разделов приходят
# английскими независимо от locale браузера — язык принадлежит анкете, а не
# сессии (проверено на живом hh.ru 26.08, ?locale=RU не переключает). Слепок
# снят с https://hh.ru/resume/016a96bb000522ca2e0039ed1f5a4c5a645465, где
# прежний разбор по одним русским подписям терял ВСЕ секции сразу.
DETAIL_EN = """
Male
Brazil, not willing to relocate, not prepared for business trips
Machine Learning Engineer
250 000 ₽ in hand
Specializations:
Programmer, developer
Employment type: full time, part time
Work format: at the employer's location, remote
Work experience 9 years 11 months
Skills
Skill proficiency levels
Advanced level
Java
Python
Medium level
Shell Scripting
Level not specified
Linux
Education
Higher education (Doctor of Science)
Languages
Portuguese — Native
English — C2 — Proficiency
Citizenship, travel time to work
Desired travel time to work: not important
"""


def test_parse_detail_reads_english_resume_sections():
    """hh.ru отдаёт подписи на языке анкеты — разбор обязан понимать обе локали.

    Регрессия боевых данных: 472 из 6233 собранных резюме (7.6%) осели в базе
    пустыми по ВСЕМ секциям сразу, потому что парсер знал только «Опыт работы»
    и «Навыки». Потеря была смещена в самый технический сегмент — Machine
    Learning 34%, Data Scientist 18% против 0% у «промпт инженер».
    """
    snapshot = parse_competitor_resume_text(
        DETAIL_EN,
        resume_id="en",
        resume_url="https://hh.ru/resume/en",
        headings=[
            "250 000 ₽ in hand",
            "Work experience 9 years 11 months",
            "Skills",
            "Education",
            "Languages",
        ],
        desired_role="Machine Learning Engineer",
    )
    assert snapshot.desired_role == "Machine Learning Engineer"
    assert snapshot.experience_months == 119
    assert snapshot.specializations == ["Programmer, developer"]
    assert snapshot.employment_types == ["full time", "part time"]
    assert snapshot.work_formats == ["at the employer's location", "remote"]
    assert snapshot.education == ["Higher education (Doctor of Science)"]
    assert snapshot.languages == ["Portuguese — Native", "English — C2 — Proficiency"]
    # Уровень владения нормализован к русскому имени: значение уходит в БД и
    # отчёты, где английский дубль расщепил бы бакет.
    assert [(skill.name, skill.proficiency) for skill in snapshot.skills] == [
        ("Java", "Продвинутый уровень"),
        ("Python", "Продвинутый уровень"),
        ("Shell Scripting", "Средний уровень"),
        ("Linux", "Уровень не указан"),
    ]


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Опыт работы 5 лет 3 месяца", 63),
        ("Work experience 9 years 11 months", 119),
        ("Work experience 1 year 1 month", 13),
        ("Work experience 6 months", 6),
        ("Опыт работы", None),
    ],
)
def test_experience_months_parses_both_locales(heading, expected):
    assert _months(heading) == expected


def test_search_url_is_keyword_only_and_page_numbered():
    url = build_competitor_search_url("AI Engineer", 2)
    assert "text=AI+Engineer" in url
    assert "page=2" in url
    assert "items_on_page=100" in url
    assert "resume=" not in url

    smoke_url = build_competitor_search_url("AI Engineer", 0, items_per_page=20)
    assert "items_on_page=20" in smoke_url


def test_search_url_defaults_to_position_scope():
    """Замер живой выдачи 26.08 по «AI»: position 619 / keywords ~3800 /
    full_text ~5000, где у full_text топ-роль «Графический дизайнер» (~81%
    мусора: `.ai` — формат Adobe Illustrator в навыках). Дефолт обязан
    оставаться `position`, иначе сбор снова наберёт дизайнеров."""
    assert "pos=position" in build_competitor_search_url("AI", 0)


@pytest.mark.parametrize("scope", ["full_text", "position", "keywords"])
def test_search_url_supports_every_hh_search_scope(scope):
    """Все три области hh.ru должны быть доступны явным выбором."""
    assert f"pos={scope}" in build_competitor_search_url("AI", 0, search_in=scope)


def test_search_url_rejects_unknown_search_in():
    with pytest.raises(ValueError, match="search_in"):
        build_competitor_search_url("AI", 0, search_in="everywhere")


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


@pytest.mark.parametrize(
    ("raw", "area", "business_trips"),
    [
        ("Екатеринбург • Не готов к командировкам", "Екатеринбург", "Не готов к командировкам"),
        (
            "Подольск (Московская область) • Не готов к командировкам",
            "Подольск (Московская область)",
            "Не готов к командировкам",
        ),
        ("Москва • Готова к редким командировкам", "Москва", "Готова к редким командировкам"),
        (None, None, None),
        ("—", None, None),
    ],
)
def test_parse_search_area_and_business_trips(raw, area, business_trips):
    assert parse_search_area_and_business_trips(raw) == (area, business_trips)


def test_parse_detail_business_trips_and_metro():
    assert parse_detail_business_trips_and_metro(
        "Москва, м. Тверская, не готова к командировкам"
    ) == ("не готова к командировкам", "Тверская")


def test_parse_detail_business_trips_and_metro_accepts_missing_header():
    assert parse_detail_business_trips_and_metro(None) == (None, None)


def test_parse_detail_extracts_only_competitor_fields():
    snapshot = parse_competitor_resume_text(
        DETAIL,
        resume_id="abc",
        resume_url="https://hh.ru/resume/abc",
        headings=[
            "200 000 ₽ на руки",
            "Опыт работы 5 лет 3 месяца",
            "Навыки",
            "Образование",
            "Знание языков",
        ],
        desired_role="AI Engineer / AI Infrastructure Engineer",
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
    """A blank desired_role (e.g. an empty h1) must fail closed."""
    with pytest.raises(CompetitorResumeIndeterminate, match="desired_role"):
        parse_competitor_resume_text(
            "Навыки\nPython",
            resume_id="abc",
            resume_url="https://hh.ru/resume/abc",
            headings=[],
            desired_role="  ",
        )


def test_skill_values_are_persisted_without_privacy_filter():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\nНавыки\nPython\ntest@example.com\n+7 999 123-45-67\nhttps://example.com",
        resume_id="skill-contact",
        resume_url="https://hh.ru/resume/skill-contact",
        headings=["Навыки"],
        desired_role="AI Engineer",
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
        headings=["Опыт работы 4 года", "Навыки"],
        desired_role="3D Generalist - AI Generalist",
    )
    assert snapshot.desired_role == "3D Generalist - AI Generalist"
    assert snapshot.salary_from is None


def test_thin_space_salary_and_dashless_specialization_are_normalized():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\n2\u2009500\u00a0€ на\u00a0руки\nСпециализации:\nРазработчик\n"
        "Тип занятости: полная занятость\nОпыт работы 1\u00a0год",
        resume_id="thin",
        resume_url="https://hh.ru/resume/thin",
        headings=["2\u2009500\u00a0€ на\u00a0руки", "Опыт работы 1\u00a0год"],
        desired_role="AI Engineer",
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
        headings=[],
        desired_role="AI Engineer",
    )
    assert snapshot.experience_summary == "Живу в Москве, мне 35 лет"
    assert snapshot.achievements == "Сократил расходы на 20%"


def _detail_card(*, desired_role: str = "GPT") -> CompetitorSearchCard:
    return CompetitorSearchCard(
        resume_id="abc", resume_url="https://hh.ru/resume/abc", desired_role=desired_role, rank=1
    )


def _fake_detail_page(*, title_count: int, title_text: str = "GPT"):
    """A Playwright Page double for fetch_competitor_resume (#792 regression).

    Confirmed live DOM 2026-08-29 (docs/research/issue-792-live-probe.md):
    ``main h2`` never contains the desired-role title — it lives in
    ``h1[data-qa='resume-block-title-position']``. This double reproduces
    exactly that shape: DETAIL_HEADING yields only section/salary headings,
    DETAIL_TITLE_POSITION is the sole source of the title. Built from the
    same _Locator/_Text doubles as _PaginationPage below, not a bespoke
    mock — this is fetch_competitor_resume's page.locator(selector) shape.
    """
    empty = _Locator([])
    locators = {
        selectors.DETAIL_MAIN: _Locator([f"{title_text}\nНавыки\nPython"]),
        selectors.DETAIL_HEADING: _Locator(["Навыки"]),
        selectors.DETAIL_TITLE_POSITION: _Locator([title_text] * title_count),
        selectors.DETAIL_PERSONAL_ADDRESS: empty,
        selectors.DETAIL_RELOCATION: empty,
        selectors.DETAIL_PERSONAL_INFO: empty,
    }

    class _DetailPage:
        def locator(self, selector):
            return locators[selector]

    return _DetailPage()


def _patch_fetch_prerequisites(monkeypatch):
    monkeypatch.setattr(competitors_module, "goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr(competitors_module, "raise_for_antibot", lambda *_a, **_k: None)
    monkeypatch.setattr(competitors_module, "require_authenticated_page", lambda *_a, **_k: None)
    monkeypatch.setattr(competitors_module, "resume_identity_matches", lambda *_a, **_k: True)


def test_fetch_competitor_resume_prefers_card_desired_role_over_detail_h1(monkeypatch):
    """card.desired_role is already confirmed from the search listing (same
    trust model as card.area) — it must win over a fresh detail-page scrape."""
    _patch_fetch_prerequisites(monkeypatch)
    page = _fake_detail_page(title_count=1, title_text="Prompt Engineer")

    snapshot = fetch_competitor_resume(
        page, _detail_card(desired_role="GPT"), require_authentication=False
    )

    assert snapshot.desired_role == "GPT"


def test_fetch_competitor_resume_falls_back_to_h1_when_card_role_missing(monkeypatch):
    """Regression for #792: DETAIL_HEADING (main h2) never carries the
    desired-role title, only DETAIL_TITLE_POSITION (h1) does. Exercised via
    the fallback path, since a normally-parsed card always has desired_role."""
    _patch_fetch_prerequisites(monkeypatch)
    page = _fake_detail_page(title_count=1, title_text="GPT")

    snapshot = fetch_competitor_resume(
        page, _detail_card(desired_role=""), require_authentication=False
    )

    assert snapshot.desired_role == "GPT"


def test_fetch_competitor_resume_fails_closed_when_title_missing(monkeypatch):
    """No card role and no confirmed h1 title -> fail closed."""
    _patch_fetch_prerequisites(monkeypatch)
    page = _fake_detail_page(title_count=0)

    with pytest.raises(CompetitorResumeIndeterminate, match="desired_role"):
        fetch_competitor_resume(page, _detail_card(desired_role=""), require_authentication=False)


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

    def all_inner_texts(self):
        return list(self.values)

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
