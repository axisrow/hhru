"""Тесты общего модуля чтения идентификаторов резюме (resume_ids, #891).

Модуль — только чтения (URL regex / data-qa карточки / SSR applicantResumes),
поэтому все тесты идут на фейках без браузера. Регресс-цель рефакторинга #891:
три мутационные команды (create/copy/delete-resume) читают identity из одного
места, и каждое чтение возвращает «не доказано» (пустая строка/причина), а не
догадку, когда источник недоступен.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hhru_bot.resume_ids import (
    RESUME_ID_FROM_PATH_OR_QUERY_RE,
    RESUME_ID_FROM_PATH_RE,
    card_resume_id,
    page_card_hashes,
    read_ssr_resume_items,
    resume_card_locator,
    resume_item_attrs,
)
from hhru_bot.selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
)

pytestmark = pytest.mark.unit

# Очевидно поддельные resume_id (хвосты фикстур проекта), не реальные хэши.
ID_A = "a" * 38
ID_B = "b" * 38


class _Link:
    def __init__(self, qa: str | None):
        self._qa = qa

    def get_attribute(self, name: str) -> str | None:
        return self._qa


class _LinksLocator:
    """Локатор ссылок внутри карточки: снапшот .all(), как настоящий Playwright."""

    def __init__(self, qas: list[str | None]):
        self._qas = qas

    def all(self) -> list[_Link]:
        return [_Link(qa) for qa in self._qas]


class _Card:
    def __init__(self, qas: list[str | None]):
        self._qas = qas

    def locator(self, selector: str) -> _LinksLocator:
        assert selector.startswith("[data-qa^='resume-card-link-'"), selector
        return _LinksLocator(self._qas)


class _HashesPage:
    def __init__(self, qas: list[str | None]):
        self._qas = qas

    def locator(self, selector: str) -> _LinksLocator:
        assert selector.startswith("[data-qa^='resume-card-link-'"), selector
        return _LinksLocator(self._qas)


class _SelectorPage:
    """Фиксирует селектор, переданный в page.locator."""

    def __init__(self) -> None:
        self.selectors: list[str] = []

    def locator(self, selector: str):
        self.selectors.append(selector)
        raise AssertionError("локатор не должен использоваться в этом тесте")


def test_path_regex_reads_resume_url():
    match = RESUME_ID_FROM_PATH_RE.search(f"https://hh.ru/resume/{ID_A}?hhtmFrom=resume")
    assert match is not None
    assert match.group(1) == ID_A


def test_path_regex_rejects_query_form():
    # Разница двух regex (#891): PATH-форма НЕ признаёт query-подтверждение
    # визарда — это семантика copy-resume, где URL только кандидат.
    assert RESUME_ID_FROM_PATH_RE.search(f"https://hh.ru/applicant/resumes?resume={ID_A}") is None


def test_path_or_query_regex_reads_both_confirmation_forms():
    # /resume/<id> — прямая страница (#304)...
    path_url = f"https://hh.ru/resume/{ID_A}"
    # ...и ?resume=<id> — следующий шаг визарда, боевой прогон #778.
    query_urls = [
        f"https://hh.ru/profile/resume/educations?resume={ID_A}&hhtmFrom=wizard",
        f"https://hh.ru/profile/resume/educations?hhtmFrom=wizard&resume={ID_A}",
        f"https://hh.ru/profile/resume/educations?resume={ID_A}",
    ]
    for url in [path_url, *query_urls]:
        match = RESUME_ID_FROM_PATH_OR_QUERY_RE.search(url)
        assert match is not None, url
        assert match.group(1) == ID_A


def test_path_or_query_regex_ignores_partial_or_foreign_ids():
    # Не hex-хвост (короткий) и чужой query-параметр — не идентификатор.
    assert RESUME_ID_FROM_PATH_OR_QUERY_RE.search("https://hh.ru/resume/abc123") is None
    assert RESUME_ID_FROM_PATH_OR_QUERY_RE.search("https://hh.ru/x?resumey=1") is None


def test_card_resume_id_reads_link_tail():
    assert card_resume_id(_Card([f"resume-card-link-{ID_A}"])) == ID_A


def test_card_resume_id_skips_foreign_links_and_returns_empty():
    # Ссылки без префикса — дрейф разметки: «не доказано» (пустая строка),
    # а не частичный id.
    assert card_resume_id(_Card(["broken-link-no-prefix", f"resume-card-link-{ID_A}"])) == ID_A
    assert card_resume_id(_Card([])) == ""
    assert card_resume_id(_Card(["resume-card-link-"])) == ""
    assert card_resume_id(_Card([None])) == ""


def test_page_card_hashes_collects_unique_ids():
    qas = [f"resume-card-link-{ID_A}", f"resume-card-link-{ID_B}", f"resume-card-link-{ID_A}"]
    assert page_card_hashes(_HashesPage(qas)) == {ID_A, ID_B}


def test_page_card_hashes_ignores_foreign_links():
    assert page_card_hashes(_HashesPage(["vacancy-link-42", None])) == set()


def test_resume_card_locator_builds_identity_bound_selector():
    page = _SelectorPage()
    with pytest.raises(AssertionError):
        resume_card_locator(page, ID_A)
    expected = f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=ID_A)})"
    assert page.selectors == [expected]


def test_resume_item_attrs_reads_attributes_or_none():
    assert resume_item_attrs({"_attributes": {"hash": ID_A}}) == {"hash": ID_A}
    assert resume_item_attrs({"other": 1}) is None
    assert resume_item_attrs("не dict") is None


def _ssr_html(payload: str) -> str:
    return f"<html><template id='HH-Lux-InitialState'>{payload}</template></html>"


def test_read_ssr_resume_items_reads_section():
    payload = json.dumps(
        {
            "applicantResumes": [
                {"_attributes": {"hash": ID_A, "id": 101}},
                {"_attributes": {"hash": ID_B, "id": 102, "parentResumeId": 101}},
            ]
        }
    )
    items, reason = read_ssr_resume_items(SimpleNamespace(content=lambda: _ssr_html(payload)))
    assert reason == ""
    assert [item["_attributes"]["hash"] for item in items] == [ID_A, ID_B]


@pytest.mark.parametrize(
    "html",
    [
        "<html>proxy check</html>",  # шаблона нет
        _ssr_html("null"),  # валидный JSON не-объект (интерстишл)
        _ssr_html("[1,2]"),
        _ssr_html(json.dumps({"other": 1})),  # секции нет
        _ssr_html(json.dumps({"applicantResumes": {}})),  # не список
        _ssr_html(json.dumps({"applicantResumes": []})),  # пустая секция
    ],
)
def test_read_ssr_resume_items_reports_unavailable_not_empty(html):
    # Fail-closed контракт: недоступность возвращается причиной, никогда —
    # «пустой список за подтверждённый факт».
    items, reason = read_ssr_resume_items(SimpleNamespace(content=lambda: html))
    assert items == []
    assert reason != ""


def test_read_ssr_resume_items_survives_parse_errors():
    def _boom() -> str:
        raise ValueError("invalid JSON")

    items, reason = read_ssr_resume_items(SimpleNamespace(content=_boom))
    assert items == []
    assert reason != ""
