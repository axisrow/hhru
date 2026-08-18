"""Тесты copy_resume.list_resume_cards (#135) — стабы Page, без браузера.

Сценарии: карточки без ссылки-хэша пропускаются; заголовок читается СКОУПЛЕННЫМ
под карточку локатором (не под page — иначе при нескольких резюме взялось бы
первое совпадение на всей странице); отсутствие/неоднозначность заголовка не
роняет функцию (title="" — RESUME_LIST_CARD_TITLE НЕ ПОДТВЕРЖДЁН дампом).

Стабы моделируют strict-mode Playwright (см. грабли PR #132, test_copy_resume_browser.py):
``.first`` возвращает самостоятельный однозначный локатор, а не self.
"""

from __future__ import annotations

import json
from html import escape
from types import SimpleNamespace

import pytest

import hhru_bot.copy_resume as cr
from hhru_bot.selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_TITLE,
)

pytestmark = pytest.mark.integration

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
    """Моделирует Playwright: count() читает состояние ПРЯМО СЕЙЧАС (без
    ожидания), а .first.wait_for() ждёт появления элемента. ``cards_ref`` —
    мутируемый список-ссылка на StubPage._cards, чтобы wait_for мог "дорендерить"
    карточки, воспроизводя гонку из test_race_waits_for_card_before_declaring_empty.
    """

    def __init__(self, cards_ref: list, delayed_cards: list | None = None):
        self._cards_ref = cards_ref
        self._delayed_cards = delayed_cards

    def count(self):
        return len(self._cards_ref)

    @property
    def first(self):
        return StubFirstCard(self._cards_ref, self._delayed_cards)

    def all(self):
        return list(self._cards_ref)


class StubFirstCard:
    def __init__(self, cards_ref: list, delayed_cards: list | None):
        self._cards_ref = cards_ref
        self._delayed_cards = delayed_cards

    def wait_for(self, timeout=None):
        if self._cards_ref:
            return
        if self._delayed_cards:
            # Карточки "дорендерились" к моменту wait_for — гонка устранена.
            self._cards_ref.extend(self._delayed_cards)
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("timeout: no resume cards rendered")


class StubPage:
    def __init__(self, cards: list[StubCard], delayed_cards: list[StubCard] | None = None):
        self._cards = list(cards)
        self._delayed_cards = delayed_cards
        self.gotos: list[str] = []

    def locator(self, selector):
        if selector == RESUME_LIST_CARD:
            return StubCardsLocator(self._cards, self._delayed_cards)
        raise AssertionError(f"неожиданный page.locator: {selector}")

    def content(self):
        return ""


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


def test_race_waits_for_card_before_declaring_empty(monkeypatch):
    """Codex adversarial review (PR #136): Locator.all() резолвит немедленно, не
    ждёт. Если карточки ещё не отрендерились в момент первого count()==0 (медленный
    рендер /applicant/resumes), list_resume_cards должна дождаться их появления
    через wait_for, а не молча отчитаться о пустом аккаунте."""
    page = StubPage([], delayed_cards=[StubCard(ID_A, title_text="Backend developer")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert len(cards) == 1
    assert cards[0].resume_id == ID_A


def test_timeout_raises_indeterminate_not_empty_list(monkeypatch):
    """Codex adversarial review (PR #136, round 2): без карточки, появившейся за
    время ожидания, состояние страницы НЕ подтверждено (timeout, анти-бот/
    интерстишл-страница, дрейф селектора — неотличимы от честно пустого
    аккаунта). list_resume_cards не должна молча возвращать [] — это выдало бы
    неопределённость за факт "резюме нет"."""
    page = StubPage([])  # без delayed_cards: wait_for кидает TimeoutError
    _patch_goto(monkeypatch, page)

    with pytest.raises(cr.ResumeListIndeterminate):
        cr.list_resume_cards(page)


def test_navigate_false_skips_goto(monkeypatch):
    """#147 (Codex adversarial review, PR #152): list_resumes.py --remote должен
    сам перейти на RESUMES_LIST_URL ДО browser.has_login_form(page) — иначе эта
    проверка читает DOM ещё не навигированной страницы и всегда возвращает False
    независимо от реального состояния сессии. navigate=False позволяет вызывающему
    коду сделать goto самостоятельно и не переходить туда же повторно здесь."""
    page = StubPage([StubCard(ID_A, title_text="Backend developer")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page, navigate=False)

    assert len(cards) == 1
    assert page.gotos == []  # goto_hh НЕ вызван — вызывающий уже перешёл сам


def test_lists_cards_with_status_from_ssr(monkeypatch):
    """#315: статус резюме читается из SSR и добавляется в карточку."""
    state = {
        "applicantResumes": [
            {"_attributes": {"hash": ID_A, "id": "284561395", "status": "not_finished"}},
            {"_attributes": {"hash": ID_B, "id": "96223331", "status": "modified"}},
        ]
    }
    html = (
        f"<html><body><template id='HH-Lux-InitialState'>"
        f"{escape(json.dumps(state, ensure_ascii=False))}</template></body></html>"
    )
    page = StubPage(
        [StubCard(ID_A, title_text="Должность не указана"), StubCard(ID_B, title_text="Backend developer")]
    )
    _patch_goto(monkeypatch, page)
    monkeypatch.setattr(page, "content", lambda: html)

    cards = cr.list_resume_cards(page)

    assert len(cards) == 2
    assert cards[0].status == "not_finished"
    assert cards[1].status == "modified"


def test_missing_ssr_does_not_fail(monkeypatch):
    """Без SSR статус резюме остаётся None — падения быть не должно."""
    page = StubPage([StubCard(ID_A, title_text="Backend developer")])
    _patch_goto(monkeypatch, page)

    cards = cr.list_resume_cards(page)

    assert len(cards) == 1
    assert cards[0].status is None


# --- resolve_numeric_resume_ids (#212) ----------------------------------------
#
# Маппинг «хэш резюме → числовой id» из SSR /applicant/resumes: Applicant
# Resumes[]._attributes.{hash,id,status}. Формы — с живой пробы 2026-08-16
# (data/logs/probe212_*.json), хэши обезличены: python-резюме там в статусе
# not_finished (форма отклика его не предлагает), marketing — рабочее
# default-резюме аккаунта.


class _NoMatches:
    def count(self):
        return 0


class StubResumesPage:
    """Page для SSR-чтения: goto + content + cookies; локаторы пусты
    (has_login_form на авторизованной странице возвращает 0 совпадений)."""

    def __init__(self, html: str = "", authed: bool = True):
        self._html = html
        self._authed = authed
        self.gotos: list[str] = []

    def goto(self, url, wait_until=""):  # noqa: ARG002
        self.gotos.append(url)

    def content(self):
        return self._html

    def locator(self, selector):  # noqa: ARG002
        return _NoMatches()

    @property
    def context(self):
        cookies = [{"name": "hhtoken", "value": "x"}] if self._authed else []
        return SimpleNamespace(cookies=lambda: cookies)


def _resumes_ssr_html(resumes: list[dict]) -> str:
    state = json.dumps({"applicantResumes": resumes}, ensure_ascii=False)
    return (
        f"<html><body><template id='HH-Lux-InitialState'>{escape(state)}</template></body></html>"
    )


def _resume_attrs(hash_: str, numeric_id: str, status: str = "modified") -> dict:
    return {"_attributes": {"hash": hash_, "id": numeric_id, "status": status}}


_HASH_PY = "c0ffee" * 5 + "b3236e"
_HASH_MK = "6b85a1" * 5 + "6e370"


def test_resolve_numeric_resume_ids_maps_hash_to_id():
    page = StubResumesPage(
        _resumes_ssr_html(
            [
                _resume_attrs(_HASH_PY, "284561395", "not_finished"),
                _resume_attrs(_HASH_MK, "96223331"),
            ]
        )
    )
    mapping = cr.resolve_numeric_resume_ids(page)
    assert mapping == {_HASH_PY: "284561395", _HASH_MK: "96223331"}
    assert mapping.statuses == {_HASH_PY: "not_finished", _HASH_MK: "modified"}
    assert page.gotos == [cr.RESUMES_LIST_URL]  # один goto за вызов


def test_resolve_numeric_resume_ids_warns_on_not_finished(caplog):
    # not_finished-резюме форма отклика не предлагает: отклики уходят с другого
    # резюме аккаунта — это обязано попасть в логи, а не пройти молча.
    page = StubResumesPage(
        _resumes_ssr_html([_resume_attrs(_HASH_PY, "284561395", "not_finished")])
    )
    with caplog.at_level("WARNING", logger="hhru_bot.copy_resume"):
        mapping = cr.resolve_numeric_resume_ids(page)
    assert mapping == {_HASH_PY: "284561395"}
    assert any("not_finished" in r.message for r in caplog.records)


def test_resolve_numeric_resume_ids_skips_entries_without_ids():
    page = StubResumesPage(
        _resumes_ssr_html(
            [
                {"no_attributes": True},
                _resume_attrs(_HASH_PY, "284561395"),
                {"_attributes": {"hash": _HASH_MK}},  # без id
                {"_attributes": {"id": "111"}},  # без hash
            ]
        )
    )
    assert cr.resolve_numeric_resume_ids(page) == {_HASH_PY: "284561395"}


def test_resolve_numeric_resume_ids_none_without_session():
    # Сессия истекла — «атрибуция недоступна» (None), не «резюме нет».
    page = StubResumesPage(_resumes_ssr_html([_resume_attrs(_HASH_PY, "284561395")]), authed=False)
    assert cr.resolve_numeric_resume_ids(page) is None


def test_resolve_numeric_resume_ids_none_without_ssr_state():
    # Страница без HH-Lux-InitialState (анти-бот/интерстишл) — None, не пустой
    # маппинг: пустой dict лгал бы «у аккаунта нет резюме».
    assert cr.resolve_numeric_resume_ids(StubResumesPage("<html>proxy check</html>")) is None


def test_resolve_numeric_resume_ids_none_without_section():
    page = StubResumesPage(
        "<html><template id='HH-Lux-InitialState'>{\"other\":1}</template></html>"
    )
    assert cr.resolve_numeric_resume_ids(page) is None


def test_resolve_numeric_resume_ids_none_on_non_dict_ssr_state():
    # parse_initial_state возвращает любой валидный JSON, не только объект:
    # null/массив/строка (schema-drift, интерстишл) не должны ронять apply
    # AttributeError'ом вне try — нормализуются как «маппинг недоступен».
    for raw in ("null", "[1,2]", '"строка"'):
        page = StubResumesPage(f"<html><template id='HH-Lux-InitialState'>{raw}</template></html>")
        assert cr.resolve_numeric_resume_ids(page) is None
