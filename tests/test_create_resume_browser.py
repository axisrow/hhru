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
from hhru_bot.create_resume import _select_catalog_leaf as _real_select
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


class HydrationRacePage(Page):
    """Карточка визарда отрисована SSR, но React привязывает обработчик позже.

    Живая разведка #778: ``[data-qa='resume-profile-card-select-job']`` — это
    ``<div role="button">``, видимый сразу, но без ``__react*`` ключей ещё
    несколько секунд. Клик по нему проходит без ошибки и молча не даёт
    эффекта: экран не переключается, поле ввода должности не появляется.
    """

    def __init__(self, clicks_until_hydrated=1):
        super().__init__(fail_on_wait=None)
        self.clicks_until_hydrated = clicks_until_hydrated
        self.select_job_clicks = 0

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        if not isinstance(url, str):
            # Финальное ожидание URL нового резюме — сохранение прошло.
            self.url = f"https://hh.ru/resume/{'b' * 38}"
        return None

    @property
    def _screen_switched(self):
        return self.select_job_clicks > self.clicks_until_hydrated

    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if selector == RESUME_CREATION_POSITION and not self._screen_switched:
            count = 0  # экран не переключился — поля на странице нет
        return HydrationLocator(self, selector, count)


class HydrationLocator(Locator):
    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self.count() != 1:
            raise PlaywrightError(f"Timeout {timeout}ms exceeded waiting for {self.selector}")

    def click(self, *, timeout=None):  # noqa: ARG002
        self.page.clicks.append(self.selector)
        if self.selector == RESUME_CREATION_SELECT_JOB:
            self.page.select_job_clicks += 1


def test_click_on_unhydrated_card_is_retried(monkeypatch):
    """Клик по ещё не гидратированной карточке должен быть повторён.

    Живой прогон 2026-08-29: 3/3 попытки кликнуть сразу после
    ``state="visible"`` не переключали экран (POSITION=0), 3/3 попытки после
    ожидания гидратации срабатывали (POSITION=1). Видимость SSR-разметки не
    означает готовность React-обработчика, поэтому одного ``wait_for`` мало.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = HydrationRacePage(clicks_until_hydrated=1)
    reserved: list[str] = []

    result = _run(page, before_click=lambda: reserved.append("clicked"))

    assert page.select_job_clicks >= 2, "первый клик ушёл в пустоту — нужен повтор"
    assert result.success, f"визард должен пройти после повторного клика: {result.reason}"


def test_permanently_unhydrated_card_fails_without_uncertain(monkeypatch):
    """Ретрай ограничен: экран так и не переключился — честный failed.

    Бесконечный повтор превратил бы сломанный визард в зависание, а
    ``uncertain`` здесь недопустим: сохраняющий клик не выполнялся (#777).
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = HydrationRacePage(clicks_until_hydrated=99)
    reserved: list[str] = []

    result = _run(page, before_click=lambda: reserved.append("clicked"))

    assert not result.success
    assert not result.uncertain
    assert not reserved, "сохраняющий клик не должен быть достигнут"
    assert "не переключился" in result.reason
    assert page.select_job_clicks == 3, "ровно три попытки, без бесконечного цикла"


class CatalogFilterPage(HydrationRacePage):
    """Дерево каталога фильтруется асинхронно: до применения фильтра в нём
    остаётся полный список, где точного совпадения с ``AREA`` нет.

    Живой замер #778: до ``fill`` — 14 узлов, сразу после ``wait_for`` первого
    узла — те же 14 (старый список), и лишь через ~500 мс React оставляет 1.
    """

    TREE = "tree-selector-item-text-"

    def __init__(self, reads_until_filtered=1):
        super().__init__(clicks_until_hydrated=0)
        self.reads_until_filtered = reads_until_filtered
        self.tree_reads = 0

    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if self.TREE in selector:
            return CatalogLocator(self, selector, count)
        return HydrationLocator(self, selector, count)


class CatalogLocator(HydrationLocator):
    def all(self):
        self.page.tree_reads += 1
        if self.page.reads_until_filtered is None:
            # Профессии нет в каталоге: фильтр не даст совпадения никогда.
            return [TreeItem("Аналитик", "tree-selector-item-text-10")]
        if self.page.tree_reads <= self.page.reads_until_filtered:
            # Ещё не отфильтровано: чужие профессии, точного совпадения нет.
            return [
                TreeItem("Аналитик", "tree-selector-item-text-10"),
                TreeItem("Тестировщик", "tree-selector-item-text-11"),
            ]
        return [TreeItem(AREA, "tree-selector-item-text-96")]


def test_catalog_tree_is_read_after_filter_applies(monkeypatch):
    """Дерево читается после применения фильтра, а не на старом списке.

    Боевой прогон 2026-08-29 падал с «профессия не найдена однозначно
    (совпадений: 0)»: ``wait_for`` первого узла проходил мгновенно на ещё
    нефильтрованном дереве, и ``.all()`` собирал чужие профессии.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = CatalogFilterPage(reads_until_filtered=1)

    result = _run(page, before_click=lambda: None)

    assert result.success, f"каталог должен дождаться фильтрации: {result.reason}"


def test_absent_profession_still_fails_without_hanging(monkeypatch):
    """Профессии нет в каталоге — честный failed, а не бесконечный опрос."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    # Дерево НИКОГДА не отдаёт совпадение: опрос должен упереться в дедлайн.
    page = CatalogFilterPage(reads_until_filtered=None)
    # Короткий дедлайн: проверяется факт опроса и выхода, а не длительность.
    monkeypatch.setattr(
        create,
        "_select_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, filter_timeout=0.5),
    )

    result = _run(page, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена однозначно" in result.reason
    assert page.tree_reads > 1, "дерево должно перечитываться, а не читаться один раз"


def test_polling_stops_as_soon_as_match_appears(monkeypatch):
    """Найдя совпадение, опрос прекращается сразу, а не крутится до дедлайна.

    Без раннего выхода результат тот же, но каждая профессия стоила бы полного
    таймаута фильтрации на боевом прогоне.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = CatalogFilterPage(reads_until_filtered=1)

    result = _run(page, before_click=lambda: None)

    assert result.success
    # 1 чтение нефильтрованного дерева + 1 чтение с совпадением = выход.
    assert page.tree_reads == 2, f"лишние чтения после совпадения: {page.tree_reads}"
