"""Интеграционные тесты дубль-гарда create-resume (create_resume + resume_titles).

Дубль-гард «второе резюме с той же должностью создать нельзя» (#304, циклы
Codex-review 2/3) с #911 живёт в общем модуле ``resume_titles`` (чистая
проверка — в test_resume_titles.py, вместе с остальными входами записи
должности). Здесь проверяется проводка: ``create_resume_on_hh`` отклоняет
дубликат должности ДО первого клика, и отказ hh.ru при этом невидим (живая
проверка пользователя 2026-09-01: дубликат 1 в 1 молча не сохраняется).

Список резюме гоняется по фикстуре, редуцированной из живого SSR-дампа
/applicant/my_resumes (#911).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _fakes import FakeLocator, _parse_root, _parse_selector
from hhru_bot.create_resume import create_resume_on_hh

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "resume_list_titles_911.html"


class _ClickthroughLocator(FakeLocator):
    """Кликабельный локатор: дубль-гард срабатывает раньше, визард не нужен.

    ``click``/``is_disabled`` — no-op, чтобы поток дошёл до якоря визарда и
    остановился там штатным отказом «визард не отрисовался» (FakeLocator.wait_for
    поднимает Timeout на отсутствующем элементе).
    """

    def click(self, *, timeout=None) -> None:  # noqa: ARG002
        return None

    def is_disabled(self) -> bool:
        return False

    @property
    def first(self) -> FakeLocator:
        # Базовый FakeLocator.first теряет подкласс; сохранить кликабельность
        # у выбранного элемента (кнопка создания адресуется через .first).
        matches = self._resolved()
        return _ClickthroughLocator(
            self._root, self._qa_match, matches=[matches[0]] if matches else []
        )


class ListPage:
    """Двойник страницы списка резюме для проверки дубль-гарда."""

    def __init__(self, html: str):
        self._root = _parse_root(html)
        self.url = ""

    def locator(self, selector: str) -> FakeLocator:
        return _ClickthroughLocator(self._root, _parse_selector(selector))

    def goto(self, url, *, wait_until=None, timeout=None):  # noqa: ANN001, ARG002
        self.url = url

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ANN001, ARG002
        return None


def test_duplicate_title_is_refused_before_any_click():
    result = create_resume_on_hh(
        ListPage(FIXTURE.read_text(encoding="utf-8")),
        area="Программист, разработчик",
        title="Программист",
        dry_run=False,
    )
    assert not result.success
    assert not result.uncertain
    assert "уже существует" in result.reason


def test_fresh_title_is_not_refused_by_duplicate_guard():
    # Дубль-гард не блокирует новый заголовок: отказ приходит позже (визард),
    # а не от дубль-гарда. Двойник не реализует визард — достаточно
    # подтверждения, что причина отказа не про дубль.
    result = create_resume_on_hh(
        ListPage(FIXTURE.read_text(encoding="utf-8")),
        area="Программист, разработчик",
        title="Аналитик данных",
        dry_run=False,
    )
    assert "уже существует" not in (result.reason or "")


def test_unreadable_list_fails_closed():
    # Карточка без названия (дрейф разметки) — список не доказывает отсутствие
    # дубля, создание обязано отказать, а не молча разрешить.
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "<div data-qa='resume-title'><h3 data-qa='title'>QA инженер тестировщик</h3></div>",
        "",
    )
    result = create_resume_on_hh(
        ListPage(html), area="Программист, разработчик", title="Аналитик данных", dry_run=False
    )
    assert not result.success
    assert "не удалось прочитать заголовки" in result.reason
