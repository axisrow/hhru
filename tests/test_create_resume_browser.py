"""Классификация исходов create_resume_on_hh по точке невозврата (#777).

``uncertain`` означает «мутация могла состояться, проверьте hh.ru вручную» и
блокирует повторный запуск (CLAUDE.md, раздел 6). Точка невозврата визарда —
последний клик «продолжить после каталога», единственный, которому передан
``before_click``. Всё, что раньше — выбор карточки, ввод должности, первый
NEXT, выбор leaf — мутацию не совершает, и ошибка там обязана быть обычным
``failed``.
"""

from __future__ import annotations

from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

import hhru_bot.create_resume as create
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD
from hhru_bot.selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_NEXT,
    RESUME_CREATION_POSITION,
    RESUME_CREATION_SELECT_JOB,
)

pytestmark = pytest.mark.integration

TITLE = "QA Test Resume"
AREA = "Программист, разработчик"


class TreeItem:
    """Пункт каталога профессий: ``.all()`` отдаёт обёртку с data-qa leaf."""

    def __init__(self, text, qa):
        self._text = text
        self._qa = qa

    def text_content(self):
        return self._text

    def get_attribute(self, name):
        return self._qa if name == "data-qa" else None


class Locator:
    def __init__(self, page, selector, count=1):
        self.page = page
        self.selector = selector
        self._count = count

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def all_text_contents(self):
        return []

    def all(self):
        # Дерево каталога профессий: ровно одна leaf, совпадающая с AREA.
        if "tree-selector-item-text-" in self.selector:
            return [TreeItem(AREA, "tree-selector-item-text-96")]
        return [self]

    def text_content(self):
        return ""

    def get_attribute(self, name):  # noqa: ARG002
        return None

    def check(self):
        self.page.clicks.append(f"check:{self.selector}")

    def locator(self, selector):
        return Locator(self.page, selector, 0)

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self.selector == self.page.fail_on_wait:
            raise PlaywrightError(f"Timeout {timeout}ms exceeded waiting for {self.selector}")

    def click(self, *, timeout=None):  # noqa: ARG002
        self.page.clicks.append(self.selector)

    def fill(self, value):
        self.page.filled.append((self.selector, value))


class Page:
    """Двойник страницы: визард доходит до шага, заданного ``fail_on_wait``."""

    def __init__(self, fail_on_wait):
        self.fail_on_wait = fail_on_wait
        self.clicks: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.url = "https://hh.ru/profile/resume/professional_role"

    def goto(self, url, *, timeout=None, wait_until=None, referer=None):  # noqa: ARG002
        self.url = url

    def locator(self, selector):
        # Пустой аккаунт: карточек резюме нет, дубль-гард пропускает создание.
        count = 0 if selector == RESUME_LIST_CARD else 1
        return Locator(self, selector, count)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        return None

    def wait_for_timeout(self, timeout):  # noqa: ARG002
        return None


def _run(page, before_click=None):
    return create.create_resume_on_hh(
        cast(PlaywrightPage, cast(object, page)),
        area=AREA,
        title=TITLE,
        dry_run=False,
        before_click=before_click,
    )


@pytest.mark.parametrize(
    "fail_selector,label",
    [
        (RESUME_CREATION_POSITION, "поле ввода должности"),
        (RESUME_CREATION_NEXT, "кнопка продолжения визарда"),
    ],
)
def test_pre_click_playwright_error_is_plain_failure(monkeypatch, fail_selector, label):
    """Сбой ДО точки невозврата — обычный failed, без ложного 'uncertain'.

    Живой прогон #778 падал на ``resume-profile-position-input`` и рапортовал
    ``[FAIL] (uncertain) ошибка после клика сохранения``, хотя резюме не
    создавалось: сохраняющий клик не выполнялся вовсе.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = Page(fail_on_wait=fail_selector)
    reserved: list[str] = []

    result = _run(page, before_click=lambda: reserved.append("clicked"))

    assert not result.success
    assert not reserved, "before_click не должен вызываться до точки невозврата"
    assert not result.uncertain, f"{label}: сбой до мутации помечен uncertain"
    assert "после клика сохранения" not in result.reason, (
        f"{label}: причина утверждает, что сохранение уже кликнуто: {result.reason}"
    )


def test_list_screen_failure_stays_plain_failure(monkeypatch):
    """Контроль: самый ранний сбой (список резюме) и так не uncertain."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = Page(fail_on_wait=RESUME_CREATE_BUTTON)

    result = _run(page)

    assert not result.success
    assert not result.uncertain
    assert "список резюме не отрисовался" in result.reason


def test_wizard_screen_failure_stays_plain_failure(monkeypatch):
    """Контроль: визард не отрисовался — тоже до мутации."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = Page(fail_on_wait=RESUME_CREATION_SELECT_JOB)

    result = _run(page)

    assert not result.success
    assert not result.uncertain
    assert "визард не отрисовался" in result.reason


def test_failure_after_point_of_no_return_stays_uncertain(monkeypatch):
    """Сбой ПОСЛЕ сохраняющего клика обязан остаться uncertain (fail-closed).

    Клик мог создать резюме, результат не наблюдаем — повтор вслепую дал бы
    второе резюме, поэтому команда блокируется до ручной проверки.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))

    class UrlFailPage(Page):
        def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
            # Первый вызов открывает визард по строковому шаблону; падать
            # должен только финальный — ожидание URL нового резюме (regex).
            if isinstance(url, str):
                return None
            raise PlaywrightError("Timeout 15000ms exceeded waiting for resume URL")

    page = UrlFailPage(fail_on_wait=None)
    reserved: list[str] = []

    result = _run(page, before_click=lambda: reserved.append("clicked"))

    assert not result.success
    assert reserved == ["clicked"], "сохраняющий клик должен быть зарезервирован"
    assert result.uncertain
    assert "после клика сохранения" in result.reason


def test_missing_resume_id_after_save_stays_uncertain(monkeypatch):
    """Сохранение прошло, но resume_id не подтверждён — тоже uncertain."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = Page(fail_on_wait=None)
    reserved: list[str] = []

    result = _run(page, before_click=lambda: reserved.append("clicked"))

    assert not result.success
    assert reserved == ["clicked"]
    assert result.uncertain
    assert "не подтверждён" in result.reason
