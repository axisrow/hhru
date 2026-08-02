"""Тесты copy_resume.list_resume_cards (#135) — стабы Page, без браузера.

Сценарии: карточки без ссылки-хэша пропускаются; заголовок читается СКОУПЛЕННЫМ
под карточку локатором (не под page — иначе при нескольких резюме взялось бы
первое совпадение на всей странице); отсутствие/неоднозначность заголовка не
роняет функцию (title="" — RESUME_LIST_CARD_TITLE НЕ ПОДТВЕРЖДЁН дампом).

Стабы моделируют strict-mode Playwright (см. грабли PR #132, test_copy_resume_browser.py):
``.first`` возвращает самостоятельный однозначный локатор, а не self.
"""

from __future__ import annotations

import hhru_bot.copy_resume as cr
from hhru_bot.selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_TITLE,
)

ID_A = "a" * 38
ID_B = "b" * 38


class StubTitleLocator:
    def __init__(self, count=1, text=""):
        self._count = count
        self._text = text

    def count(self):
        return self._count

    @property
    def first(self):
        return StubTitleLocator(count=1, text=self._text)

    def inner_text(self):
        return self._text


class StubCard:
    def __init__(self, resume_id: str | None, title_count=1, title_text=""):
        self._resume_id = resume_id
        self._title_count = title_count
        self._title_text = title_text

    def locator(self, selector):
        if selector.startswith("[data-qa^='resume-card-link-'"):
            return StubHashLinks(self._resume_id)
        if selector == RESUME_LIST_CARD_TITLE:
            return StubTitleLocator(count=self._title_count, text=self._title_text)
        raise AssertionError(f"неожиданный card.locator: {selector}")


class StubHashLinks:
    """Локатор ссылки-хэша внутри карточки (data-qa='resume-card-link-<hash>')."""

    def __init__(self, resume_id: str | None):
        self._resume_id = resume_id

    def all(self):
        if self._resume_id is None:
            return []
        return [StubLink(self._resume_id)]


class StubLink:
    def __init__(self, resume_id: str):
        self._resume_id = resume_id

    def get_attribute(self, name):
        return f"resume-card-link-{self._resume_id}"


class StubCardsLocator:
    def __init__(self, cards: list[StubCard]):
        self._cards = cards

    def all(self):
        return self._cards


class StubPage:
    def __init__(self, cards: list[StubCard]):
        self._cards = cards
        self.gotos: list[str] = []

    def locator(self, selector):
        if selector == RESUME_LIST_CARD:
            return StubCardsLocator(self._cards)
        raise AssertionError(f"неожиданный page.locator: {selector}")


def _patch_goto(monkeypatch, page):
    monkeypatch.setattr(cr, "goto_hh", lambda p, url, **kw: page.gotos.append(url))


def test_lists_cards_with_hash_and_title(monkeypatch):
    page = StubPage([StubCard(ID_A, title_text="Backend developer")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert len(cards) == 1
    assert cards[0].resume_id == ID_A
    assert cards[0].title == "Backend developer"
    assert cards[0].url == f"https://hh.ru/resume/{ID_A}"
    assert page.gotos == [cr.RESUMES_LIST_URL]


def test_multiple_cards_each_title_scoped_to_own_card(monkeypatch):
    # Регрессия: заголовок должен браться из СВОЕЙ карточки, не первой на странице.
    page = StubPage(
        [
            StubCard(ID_A, title_text="Backend developer"),
            StubCard(ID_B, title_text="Data analyst"),
        ]
    )
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert [c.resume_id for c in cards] == [ID_A, ID_B]
    assert [c.title for c in cards] == ["Backend developer", "Data analyst"]


def test_card_without_hash_link_is_skipped(monkeypatch):
    page = StubPage([StubCard(None), StubCard(ID_A, title_text="Backend developer")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert len(cards) == 1
    assert cards[0].resume_id == ID_A


def test_missing_title_selector_does_not_fail(monkeypatch):
    # RESUME_LIST_CARD_TITLE не подтверждён дампом: 0 совпадений -> title="".
    page = StubPage([StubCard(ID_A, title_count=0)])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert cards[0].title == ""


def test_ambiguous_title_selector_does_not_fail(monkeypatch):
    # >1 совпадения внутри карточки — тоже не падаем (count() проверяется ДО .first).
    page = StubPage([StubCard(ID_A, title_count=2, title_text="ignored")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert cards[0].title == ""


def test_empty_list_returns_empty(monkeypatch):
    page = StubPage([])
    _patch_goto(monkeypatch, page)

    assert cr.list_resume_cards(page) == []
