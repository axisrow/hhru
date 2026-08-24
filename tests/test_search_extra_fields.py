"""Тесты доп. признаков карточки для статистики/ML (issue #517).

Чистая логика без браузера, по образцу test_search_publication_time.py:
_optional_text/_parse_experience против мок-локатора карточки, отсутствие
блока не должно ронять парсинг ни одного из новых полей.
"""

from __future__ import annotations

import pytest

import hhru_bot.search as search
from hhru_bot.search import VacancyCard, _optional_text, _parse_experience, _parse_metro_stations

pytestmark = pytest.mark.unit


class _TextLocator:
    def __init__(self, text: str, count: int = 1, attr: str | None = None):
        self._text = text
        self._count = count
        self._attr = attr

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_text(self):
        return self._text

    def get_attribute(self, name: str):
        if name == "data-qa":
            return self._attr
        return None


class _FixtureCard:
    """Мок карточки: отдаёт значение только по подтверждённому data-qa,
    любой другой селектор — пустой локатор (count=0), как реальный
    отсутствующий опциональный блок в разметке hh.ru.
    """

    def __init__(self, values: dict[str, tuple[str, str | None]]):
        # values: {selector: (text, attr_data_qa)}
        self._values = values

    def locator(self, selector: str):
        if selector in self._values:
            text, attr = self._values[selector]
            return _TextLocator(text, attr=attr)
        return _TextLocator("", count=0)


# --- address / is_remote -----------------------------------------------------


def test_address_reads_via_confirmed_selector():
    card = _FixtureCard({search.sel.VACANCY_CARD_ADDRESS: ("Москва", None)})
    assert _optional_text(card, search.sel.VACANCY_CARD_ADDRESS) == "Москва"


def test_address_missing_block_returns_none():
    card = _FixtureCard({})
    assert _optional_text(card, search.sel.VACANCY_CARD_ADDRESS) is None


def test_is_remote_true_when_label_present():
    card = _FixtureCard({search.sel.VACANCY_CARD_REMOTE_LABEL: ("Можно удалённо", None)})
    assert card.locator(search.sel.VACANCY_CARD_REMOTE_LABEL).first.count() > 0


def test_is_remote_false_when_label_absent():
    card = _FixtureCard({})
    assert card.locator(search.sel.VACANCY_CARD_REMOTE_LABEL).first.count() == 0


# --- experience: значение в суффиксе data-qa, не в тексте --------------------


def test_parse_experience_reads_category_from_data_qa_suffix():
    qa = "vacancy-serp__vacancy-work-experience-between1And3"
    card = _FixtureCard({search.sel.VACANCY_CARD_EXPERIENCE: ("Опыт 1-3 года", qa)})
    assert _parse_experience(card) == "between1And3"


def test_parse_experience_no_experience_category():
    qa = "vacancy-serp__vacancy-work-experience-noExperience"
    card = _FixtureCard({search.sel.VACANCY_CARD_EXPERIENCE: ("Без опыта", qa)})
    assert _parse_experience(card) == "noExperience"


def test_parse_experience_missing_block_returns_empty_string():
    card = _FixtureCard({})
    assert _parse_experience(card) == ""


def test_parse_experience_attribute_without_expected_prefix_returns_empty_string():
    # Fail-closed: если сайт когда-нибудь отдаст этот селектор с другим
    # data-qa (дрейф разметки), не подсовываем мусорное значение.
    card = _FixtureCard({search.sel.VACANCY_CARD_EXPERIENCE: ("текст", "unexpected-qa")})
    assert _parse_experience(card) == ""


def test_parse_experience_ambiguous_match_returns_empty_string():
    """Fail-closed на неоднозначности (>1 совпадение) — селектор префиксный
    ([data-qa^='...']), не точный, поэтому в отличие от однозначных полей
    карточки код НЕ полагается на .first и не выбирает произвольный элемент.
    """

    class _AmbiguousLocator:
        @property
        def first(self):
            return self

        def count(self):
            return 2

        def get_attribute(self, _name: str):
            raise AssertionError("не должен читаться при неоднозначном count()")

    class _AmbiguousCard:
        def locator(self, selector: str):
            if selector == search.sel.VACANCY_CARD_EXPERIENCE:
                return _AmbiguousLocator()
            return _TextLocator("", count=0)

    assert _parse_experience(_AmbiguousCard()) == ""


# --- snippet_requirement / snippet_responsibility -----------------------------


def test_snippet_requirement_reads_via_confirmed_selector():
    text = "Опыт работы с Hadoop. Владение Python."
    card = _FixtureCard({search.sel.VACANCY_CARD_SNIPPET_REQUIREMENT: (text, None)})
    assert _optional_text(card, search.sel.VACANCY_CARD_SNIPPET_REQUIREMENT) == text


def test_snippet_responsibility_reads_via_confirmed_selector():
    text = "Развитие и поддержка сервисов платформы."
    card = _FixtureCard({search.sel.VACANCY_CARD_SNIPPET_RESPONSIBILITY: (text, None)})
    assert _optional_text(card, search.sel.VACANCY_CARD_SNIPPET_RESPONSIBILITY) == text


def test_snippets_missing_block_returns_none():
    card = _FixtureCard({})
    assert _optional_text(card, search.sel.VACANCY_CARD_SNIPPET_REQUIREMENT) is None
    assert _optional_text(card, search.sel.VACANCY_CARD_SNIPPET_RESPONSIBILITY) is None


def test_parse_metro_stations_deduplicates_and_filters_empty_names():
    class _StationsLocator:
        def __init__(self, values):
            self.values = values

        def count(self):
            return len(self.values)

        def nth(self, index):
            return _TextLocator(self.values[index])

    class _Card:
        def locator(self, selector):
            if selector == search.sel.VACANCY_CARD_METRO_STATION:
                return _StationsLocator(["Белорусская", "", "Белорусская", "Динамо"])
            return _TextLocator("", count=0)

    assert _parse_metro_stations(_Card()) == ["Белорусская", "Динамо"]


def test_parse_metro_stations_distinguishes_missing_and_empty_blocks():
    class _EmptyCard:
        def locator(self, selector):
            if selector == search.sel.VACANCY_CARD_METRO_STATION:

                class _EmptyLocator(_TextLocator):
                    def nth(self, index):
                        assert index == 0
                        return self

                return _EmptyLocator("", count=1)
            return _TextLocator("", count=0)

    assert _parse_metro_stations(_FixtureCard({})) is None
    assert _parse_metro_stations(_EmptyCard()) == []


# --- side_job / no_resume ----------------------------------------------------


def test_optional_card_badges_are_present_when_locators_match():
    card = _FixtureCard(
        {
            search.sel.VACANCY_CARD_SIDE_JOB: ("Подработка", None),
            search.sel.VACANCY_CARD_NO_RESUME: ("Можно без резюме", None),
        }
    )
    assert card.locator(search.sel.VACANCY_CARD_SIDE_JOB).first.count() > 0
    assert card.locator(search.sel.VACANCY_CARD_NO_RESUME).first.count() > 0


def test_optional_card_badges_are_absent_when_locators_do_not_match():
    card = _FixtureCard({})
    assert card.locator(search.sel.VACANCY_CARD_SIDE_JOB).first.count() == 0
    assert card.locator(search.sel.VACANCY_CARD_NO_RESUME).first.count() == 0


# --- VacancyCard: дефолты новых полей ------------------------------------------


def test_vacancy_card_new_fields_default_to_empty():
    c = VacancyCard(vacancy_id="1", title="T", company="C", url="https://hh.ru/vacancy/1")
    assert c.address == ""
    assert c.is_remote is None
    assert c.experience == ""
    assert c.snippet_requirement == ""
    assert c.snippet_responsibility == ""
    assert c.side_job is None
    assert c.no_resume is None
    assert c.metro_stations is None


def test_vacancy_card_accepts_new_fields():
    c = VacancyCard(
        vacancy_id="1",
        title="T",
        company="C",
        url="https://hh.ru/vacancy/1",
        address="Москва",
        is_remote=True,
        experience="between1And3",
        snippet_requirement="req",
        snippet_responsibility="resp",
        side_job=True,
        no_resume=False,
        activity="Активно отвечает",
        hh_rating="4,8",
        hrbrand_winner=True,
        metro_stations=["Таганская", "Марксистская"],
    )
    assert c.address == "Москва"
    assert c.is_remote is True
    assert c.experience == "between1And3"
    assert c.snippet_requirement == "req"
    assert c.snippet_responsibility == "resp"
    assert c.side_job is True
    assert c.no_resume is False
    assert c.activity == "Активно отвечает"
    assert c.hh_rating == "4,8"
    assert c.hrbrand_winner is True
    assert c.metro_stations == ["Таганская", "Марксистская"]
