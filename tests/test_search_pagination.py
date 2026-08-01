"""Тесты детекции следующей страницы поиска (#123).

Браузер НЕ поднимается: _has_next_page — чистая функция над locator-API,
прогоняется на FakePage поверх HTML-фикстур (приём из tests/_fakes.py и
tests/test_probe_healthcheck.py).

Регрессия, которую ловим: hh.ru отдаёт пагинацию в ДВУХ вариантах разметки —
с кнопкой pager-next и без неё (контейнер с классом-модификатором
`...-without-navigation-buttons`). Признак «есть следующая страница», взятый
только от pager-next, во втором варианте молча обрывал сбор на первой
странице: 20 карточек вместо сотен. Обе вёрстки подтверждены живым
залогиненным дампом (2026-08-01).
"""

from __future__ import annotations

from _fakes import FakeLocator, _parse_root, _parse_selector
from hhru_bot.search import _has_next_page


class _FakePage:
    """Минимальный Playwright-Page: только locator(selector) над статичным DOM."""

    def __init__(self, html: str):
        self._root = _parse_root(html)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._root, _parse_selector(selector))


def _pager(pages: list[str], *, with_next: bool) -> str:
    """Строит разметку pager-block: номера страниц + опц. кнопка «вперёд»."""
    links = "".join(f"<a data-qa='pager-page'>{label}</a>" for label in pages)
    nxt = "<a data-qa='pager-next'>дальше</a>" if with_next else ""
    return f"<nav data-qa='pager-block'>{links}{nxt}</nav>"


class TestPagerNextVariant:
    """Вариант 1: вёрстка С кнопкой навигации (работала и до #123)."""

    def test_next_button_present_means_more_pages(self):
        page = _FakePage(_pager(["1", "2", "3"], with_next=True))
        assert _has_next_page(page, 0) is True

    def test_next_button_alone_is_enough(self):
        """pager-next достаточен даже без разбора номеров."""
        page = _FakePage("<nav><a data-qa='pager-next'>дальше</a></nav>")
        assert _has_next_page(page, 0) is True


class TestWithoutNavigationButtonsVariant:
    """Вариант 2: вёрстка БЕЗ кнопок — регрессия #123.

    Ровно этот случай ломал сбор: pager-next отсутствует, но страниц много.
    """

    def test_numbered_pages_detected_without_next_button(self):
        page = _FakePage(_pager(["1", "2", "3"], with_next=False))
        assert _has_next_page(page, 0) is True

    def test_detects_from_middle_page(self):
        page = _FakePage(_pager(["1", "2", "3", "4"], with_next=False))
        assert _has_next_page(page, 2) is True  # page_num=2 → UI-номер 3, есть 4

    def test_last_page_has_no_next(self):
        """На последней странице номеров больше текущего нет → стоп."""
        page = _FakePage(_pager(["1", "2", "3"], with_next=False))
        assert _has_next_page(page, 2) is False

    def test_ellipsis_label_is_ignored(self):
        """«...» между номерами не число — не должно ломать разбор."""
        page = _FakePage(_pager(["1", "2", "...", "9"], with_next=False))
        assert _has_next_page(page, 0) is True

    def test_ellipsis_only_is_not_a_page(self):
        page = _FakePage(_pager(["1", "..."], with_next=False))
        assert _has_next_page(page, 0) is False


class TestNoPagination:
    """Единственная страница выдачи — прежнее поведение (стоп)."""

    def test_no_pager_block_at_all(self):
        page = _FakePage("<div data-qa='vacancy-serp__vacancy'>карточка</div>")
        assert _has_next_page(page, 0) is False

    def test_single_page_number(self):
        page = _FakePage(_pager(["1"], with_next=False))
        assert _has_next_page(page, 0) is False
