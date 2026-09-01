"""Тесты уникальности должностей аккаунта (resume_titles, #911).

Пользователь установил вручную (2026-09-01): все должности в одном аккаунте
уникальны, дубликат 1 в 1 молча не сохраняется. Отсюда два инварианта:
``duplicate_title_reason`` — чистая fail-closed проверка (перенос дубль-гарда
create-resume #304, циклы Codex-review 2/3, в общий для всех входов модуль) и
``read_account_titles`` — чтение списка, где частичный/неидентифицируемый
список не может доказать отсутствие дубля и обязан отказывать.

Хэппи-путь ридера гоняется по фикстуре, редуцированной из ЖИВОГО SSR-дампа
/applicant/my_resumes (правило #911: фикстуры строить из реального дампа).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

from _fakes import FakeLocator, _parse_root, _parse_selector
from hhru_bot.resume_titles import (
    AccountTitle,
    account_duplicate_reason,
    duplicate_title_reason,
    read_account_titles,
)
from hhru_bot.selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_PREFIX,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "resume_list_titles_911.html"

# Очевидно поддельные resume_id фикстуры (хвосты), чтобы тесты читались без hex-стен.
ID_1 = "0" * 31 + "1"
ID_2 = "0" * 31 + "2"
ID_3 = "0" * 31 + "3"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class ListPage:
    """Двойник страницы списка резюме: только ``page.locator``/``goto``."""

    def __init__(self, html: str):
        self._root = _parse_root(html)
        self.url = ""

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._root, _parse_selector(selector))

    def goto(self, url, *, wait_until=None, timeout=None):  # noqa: ANN001, ARG002
        self.url = url


@pytest.mark.parametrize(
    ("entries", "title", "exclude", "expected"),
    [
        # Пустой аккаунт (карточек нет) — первое резюме создавать можно.
        ([], "Backend developer", "", ""),
        # Совпадение по должности → дубль запрещён.
        ([AccountTitle(ID_1, "Backend developer")], "Backend developer", "", "уже существует"),
        (
            [AccountTitle(ID_1, "QA"), AccountTitle(ID_2, "Backend developer")],
            "Backend developer",
            "",
            "уже существует",
        ),
        # Нет совпадения — разрешено.
        ([AccountTitle(ID_1, "QA")], "Backend developer", "", ""),
        # Своя карточка исключается: сохранить должность, которую резюме уже
        # носит, — не дубль (переименование в себя же / повторное сохранение).
        ([AccountTitle(ID_1, "Backend developer")], "Backend developer", ID_1, ""),
        (
            [AccountTitle(ID_1, "QA"), AccountTitle(ID_2, "Backend developer")],
            "Backend developer",
            ID_2,
            "",
        ),
        # Два одинаковых названия при исключённой одной из карточек — вторая
        # всё равно дубль: hh.ru не создаёт дубликатов, значит список подозрителен
        # и отказ безопаснее молчаливого разрешения.
        (
            [AccountTitle(ID_1, "QA"), AccountTitle(ID_2, "Backend developer")],
            "Backend developer",
            ID_1,
            "уже существует",
        ),
    ],
)
def test_duplicate_title_reason(entries, title, exclude, expected):
    reason = duplicate_title_reason(entries, title, exclude_resume_id=exclude)
    if expected:
        assert expected in reason
    else:
        # Allowed-case обязан быть строго "" (не «содержит ""»): тривиально
        # истинное утверждение маскировало бы регрессию fail-closed.
        assert reason == ""


@pytest.mark.parametrize(
    ("existing", "new", "expected"),
    [
        # normalize() (external_forms/detect): collapse-whitespace + strip + casefold.
        ("  Backend   Developer  ", "backend developer", "уже существует"),
        ("QA Automation Engineer", "qa automation engineer", "уже существует"),
    ],
)
def test_duplicate_title_reason_normalizes(existing, new, expected):
    assert expected in duplicate_title_reason([AccountTitle(ID_1, existing)], new)


def test_reader_parses_real_dump_fixture():
    page = ListPage(_fixture_html())
    entries, reason = read_account_titles(page)
    assert reason == ""
    assert [(entry.resume_id, entry.title) for entry in entries] == [
        (ID_1, "Программист"),
        (ID_2, "Программист, разработчик"),
        (ID_3, "QA инженер тестировщик"),
        ("0" * 31 + "4", "AI Engineer / Инженер агентных систем"),
    ]


def test_reader_fails_closed_when_card_link_missing():
    # Реальная фикстура с испорченной ссылкой одной карточки (дрейф разметки):
    # частичный список не может доказать отсутствие дубля.
    html = _fixture_html().replace(f"resume-card-link-{ID_2}", "broken-link-no-prefix")
    entries, reason = read_account_titles(ListPage(html))
    assert entries == []
    assert "без resume_id" in reason


def test_reader_fails_closed_when_title_missing():
    html = _fixture_html().replace(
        "<div data-qa='resume-title'><h3 data-qa='title'>QA инженер тестировщик</h3></div>",
        "",
    )
    entries, reason = read_account_titles(ListPage(html))
    assert entries == []
    assert "не удалось прочитать заголовки" in reason


def test_reader_allows_empty_account_with_create_button():
    # Пустой аккаунт: карточек нет, кнопка создания есть — легитимно пусто.
    html = (
        "<html><body><button data-qa='mainmenu_createResume'>Создать резюме</button></body></html>"
    )
    entries, reason = read_account_titles(ListPage(html))
    assert entries == []
    assert reason == ""


def test_reader_fails_closed_when_nothing_rendered():
    # Ни карточек, ни кнопки — не «пустой аккаунт», а неотрисованный экран.
    html = "<html><body></body></html>"
    entries, reason = read_account_titles(ListPage(html))
    assert entries == []
    assert "не отрисовался" in reason


def test_account_duplicate_reason_end_to_end():
    # goto + якорь «кнопка ИЛИ карточка» + чтение + проверка одним вызовом.
    reason = account_duplicate_reason(ListPage(_fixture_html()), "Программист")
    assert "уже существует" in reason
    assert account_duplicate_reason(ListPage(_fixture_html()), "Аналитик данных") == ""


def test_account_duplicate_reason_excludes_own_resume():
    reason = account_duplicate_reason(
        ListPage(_fixture_html()), "Программист", exclude_resume_id=ID_1
    )
    assert reason == ""


class _OneNode:
    """Локатор-двойник одного узла; при raise_read чтение кидает PlaywrightError.

    Моделирует ререндер между count() и чтением (ревью PR #912): html-фикстура
    на таком детач не способна, поэтому двойник duck-typed, без _fakes.
    """

    def __init__(self, *, qa: str | None = None, text: str | None = None, raise_read: bool = False):
        self._qa = qa
        self._text = text
        self._raise_read = raise_read

    @property
    def first(self) -> _OneNode:
        return self

    def count(self) -> int:
        return 1

    def get_attribute(self, name: str) -> str | None:
        if self._raise_read:
            raise PlaywrightError("detached by rerender")
        return self._qa

    def inner_text(self) -> str:
        if self._raise_read:
            raise PlaywrightError("detached by rerender")
        return self._text or ""


class _DetachedCard:
    """Карточка: ссылка читается, заголовок падает посреди чтения."""

    def __init__(self, resume_id: str):
        self._resume_id = resume_id

    def locator(self, selector: str):  # noqa: ANN202
        if selector == RESUME_LIST_CARD_LINK_PREFIX:
            return _OneNode(qa=f"resume-card-link-{self._resume_id}")
        return _OneNode(text="Программист", raise_read=True)


class _DetachedReadPage:
    """Страница с одной карточкой, у которой чтение заголовка кидает ошибку."""

    url = ""

    def __init__(self):
        self._cards = [_DetachedCard(ID_1)]

    def locator(self, selector: str):  # noqa: ANN202
        if selector == RESUME_LIST_CARD:
            return self
        return _OneNode()

    def count(self) -> int:
        return len(self._cards)

    def all(self) -> list[_DetachedCard]:
        return list(self._cards)


def test_reader_converts_dom_read_failure_into_reason():
    # PlaywrightError чтения (детач ререндером между count() и inner_text())
    # не покидает ридер: команды получают fail-closed причину, а не трейсбек
    # (ревью PR #912 — в create/copy-вызвах общего обработчика нет).
    entries, reason = read_account_titles(_DetachedReadPage())
    assert entries == []
    assert "не удалось прочитать список резюме" in reason


def test_account_duplicate_reason_survives_goto_failure(monkeypatch):
    # goto_hh пробрасывает ошибку последней попытки (#80): навигация к списку
    # тоже «проверка невозможна», а не краш команды.
    def _crash(page, url, *, ready_selector=None):  # noqa: ANN001, ARG001
        raise PlaywrightError("net::ERR_FAILED")

    monkeypatch.setattr("hhru_bot.resume_titles.goto_hh", _crash)
    reason = account_duplicate_reason(ListPage(_fixture_html()), "Программист")
    assert "не удалось открыть список резюме" in reason
