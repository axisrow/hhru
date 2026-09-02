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
from _fakes import _parse_root
from hhru_bot.create_resume import select_wizard_catalog_leaf as _real_select
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD
from hhru_bot.selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_CATEGORY_SUBMIT,
    RESUME_CREATION_NEXT,
    RESUME_CREATION_POSITION,
    RESUME_CREATION_SELECT_JOB,
)

pytestmark = pytest.mark.integration

TITLE = "QA Test Resume"
AREA = "Программист, разработчик"


class TreeItem:
    """Пункт каталога профессий: ``.all()`` отдаёт обёртку с data-qa leaf."""

    def __init__(self, text, qa, page=None):
        self._text = text
        self._qa = qa
        self.page = page

    def text_content(self):
        return self._text

    def get_attribute(self, name):
        return self._qa if name == "data-qa" else None

    def click(self, *, timeout=None):  # noqa: ARG002
        # Клик по строке профессии переключает скрытый за обёрткой чекбокс.
        if self.page is not None:
            self.page.checked = True


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
        # #837: реальный Playwright читает тексты одним batch-вызовом,
        # независимым от .all() (снимок handle-ов) — мок опирается на тот же
        # .all(), которое переопределяют производные классы, так что оба
        # вызова остаются согласованными без дублирования фикстур per-class.
        return [item.text_content() for item in self.all()]

    def all(self):
        # Дерево каталога профессий: ровно одна leaf, совпадающая с AREA.
        if "tree-selector-item-text-" in self.selector:
            return [TreeItem(AREA, "tree-selector-item-text-96", self.page)]
        return [self]

    def text_content(self):
        return ""

    def get_attribute(self, name):  # noqa: ARG002
        return None

    def is_disabled(self):
        return False

    def check(self):
        self.page.clicks.append(f"check:{self.selector}")

    def is_checked(self):
        # По умолчанию клик по строке отмечает чекбокс; страж переопределяет.
        return getattr(self.page, "checked", True)

    def locator(self, selector):
        return Locator(self.page, selector, 0)

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self.selector == self.page.fail_on_wait:
            raise PlaywrightError(f"Timeout {timeout}ms exceeded waiting for {self.selector}")

    def click(self, *, timeout=None):  # noqa: ARG002
        self.page.clicks.append(self.selector)

    def fill(self, value):
        self.page.filled.append((self.selector, value))


CREATE_BUTTON_HTML = "<button data-qa='mainmenu_createResume'>{label}</button>"


class HtmlCreateButtonLocator(Locator):
    def __init__(self, page, selector, *, count, disabled):
        super().__init__(page, selector, count=count)
        self._disabled = disabled

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self._count == 0:
            raise PlaywrightError(f"not visible: {self.selector}")

    def is_disabled(self):
        return self._disabled

    def click(self, *, timeout=None):  # noqa: ARG002
        if self._disabled:
            raise PlaywrightError("ERR_CONNECTION_RESET")
        super().click()


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


class HtmlCreateButtonPage(Page):
    """Existing create-resume fake with the list button sourced from HTML."""

    def __init__(self, html):
        super().__init__(fail_on_wait=None)
        buttons = _parse_root(html).find_all(
            tag=None, qa_match=lambda qa: qa == "mainmenu_createResume"
        )
        self.create_button_present = bool(buttons)
        self.create_button_disabled = bool(buttons and "disabled" in buttons[0].attrs)

    def locator(self, selector):
        if selector == RESUME_CREATE_BUTTON:
            return HtmlCreateButtonLocator(
                self,
                selector,
                count=int(self.create_button_present),
                disabled=self.create_button_disabled,
            )
        return super().locator(selector)


def _run(page, before_click=None):
    return create.create_resume_on_hh(
        cast(PlaywrightPage, cast(object, page)),
        area=AREA,
        title=TITLE,
        dry_run=False,
        before_click=before_click,
    )


@pytest.mark.parametrize(
    "html",
    [
        "<button data-qa='mainmenu_createResume' disabled>Создать</button>",
        "<main>список резюме</main>",
    ],
)
def test_resume_limit_is_reported_before_create_click(monkeypatch, html):
    """The list HTML exposes the hh.ru resume limit as disabled/missing button."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = HtmlCreateButtonPage(html)

    result = _run(page)

    assert not result.success
    assert not result.uncertain
    assert "лимит" in result.reason.lower()
    assert "удал" in result.reason.lower()
    assert RESUME_CREATE_BUTTON not in page.clicks


def test_active_create_button_keeps_existing_behavior(monkeypatch):
    """An enabled button still enters the existing wizard flow."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = HtmlCreateButtonPage(CREATE_BUTTON_HTML.format(label="Создать"))

    result = _run(page)

    assert RESUME_CREATE_BUTTON in page.clicks
    assert "лимит" not in result.reason.lower()


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
    def _current_items(self):
        if self.page.reads_until_filtered is None:
            # Профессии нет в каталоге: фильтр не даст совпадения никогда.
            return [TreeItem("Аналитик", "tree-selector-item-text-10", self.page)]
        if self.page.tree_reads <= self.page.reads_until_filtered:
            # Ещё не отфильтровано: чужие профессии, точного совпадения нет.
            return [
                TreeItem("Аналитик", "tree-selector-item-text-10", self.page),
                TreeItem("Тестировщик", "tree-selector-item-text-11", self.page),
            ]
        return [TreeItem(AREA, "tree-selector-item-text-96", self.page)]

    def all_text_contents(self):
        # #837: боевой код читает all_text_contents() один раз за итерацию
        # (используется для решения "нашли/не нашли"), затем .all() отдельным
        # вызовом на том же (уже стабилизированном для этой итерации) снимке.
        # tree_reads считает итерации опроса, а не количество вызовов метода —
        # ровно то, что боевой код измеряет одним logical "poll".
        self.page.tree_reads += 1
        return [item.text_content() for item in self._current_items()]

    def all(self):
        return self._current_items()


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


class StaleHandleTreeItem(TreeItem):
    """Элемент дерева, чей ``.text_content()`` кидает detached-ошибку (#837).

    Живой прогон 2026-08-30 воспроизвёл ровно это: между ``.all()`` (снимок
    handle-ов) и построчным ``.text_content()`` React успевает перерендерить
    дерево, и чтение на уже отсоединённом handle висит полный таймаут
    Playwright. ``all_text_contents()`` — batch-вызов на самом селекторе,
    а не по хэндлу — этой проблеме не подвержен, что и моделирует разница
    между ``self.all()`` (возвращает эти "хрупкие" объекты) и
    ``self.all_text_contents()`` (batch, всегда успешен) ниже.
    """

    def text_content(self):
        raise PlaywrightError("Locator.text_content: Timeout 30000ms exceeded waiting for element")


class StaleSnapshotLocator(HydrationLocator):
    """Первая итерация опроса отдаёт handle, ЧТЕНИЕ КОТОРОГО падает (#837);
    вторая — уже стабилизированный единственный leaf.

    Только ``.all()`` (снимок хэндлов, как в старом коде) уязвим:
    ``StaleHandleTreeItem.text_content()`` кидает исключение при построчном
    чтении. ``all_text_contents()`` (batch, как в новом коде) читает текст
    напрямую из ``TreeItem`` без похода через уязвимый ``.text_content()``
    хэндла — эта гонка для него структурно недостижима, а не просто "не
    воспроизвелась в этом прогоне". Детерминированно: ВСЕГДА падает на
    построчном чтении, ВСЕГДА проходит на batch-чтении.

    Счётчик читок живёт на ``page``, а не на этом locator'е: боевой код
    вызывает ``page.locator(selector)`` заново на каждой итерации опроса
    (свежий объект locator), поэтому состояние "какая по счёту итерация"
    обязано пережить пересоздание locator'а — тот же паттерн, что уже
    использует ``CatalogLocator`` через ``self.page.tree_reads``.
    """

    def _stale_first_read(self):
        return [
            StaleHandleTreeItem("Аналитик", "tree-selector-item-text-10", self.page),
            TreeItem(AREA, "tree-selector-item-text-96", self.page),
        ]

    def _stable_read(self):
        return [TreeItem(AREA, "tree-selector-item-text-96", self.page)]

    def _current_items(self):
        return self._stale_first_read() if self.page.stale_reads == 0 else self._stable_read()

    def all(self):
        # Боевой код вызывает all_text_contents() и .all() в одной и той же
        # логической итерации опроса — счётчик увеличивается только в
        # all_text_contents(), чтобы .all() внутри той же итерации видел то
        # же самое состояние (иначе он "перескочил" бы вперёд).
        return self._current_items()

    def all_text_contents(self):
        # Batch-чтение — не по хэндлу, значит StaleHandleTreeItem.text_content()
        # здесь никогда не вызывается: читаем прямо готовый текст элемента.
        items = self._current_items()
        self.page.stale_reads += 1
        return [item._text for item in items]  # noqa: SLF001 — тестовый двойник, не production API


class StaleSnapshotPage(CatalogFilterPage):
    def __init__(self):
        super().__init__(reads_until_filtered=0)
        self.stale_reads = 0

    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if self.TREE in selector:
            return StaleSnapshotLocator(self, selector, count)
        return HydrationLocator(self, selector, count)


def test_stale_handle_during_row_by_row_read_does_not_crash_polling(monkeypatch):
    """#837: построчное чтение .text_content() на снимке .all() падает на
    устаревшем handle — опрос обязан пережить это и продолжить, не
    пробрасывать исключение наружу как "ошибка до сохранения резюме"."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = StaleSnapshotPage()

    result = _run(page, before_click=lambda: None)

    assert result.success, f"batch-чтение не должно падать на устаревшем handle: {result.reason}"


def test_absent_profession_still_fails_without_hanging(monkeypatch):
    """Профессии нет в каталоге — честный failed, с перечнем предложенного (#836)."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    # Дерево НИКОГДА не отдаёт совпадение: опрос должен упереться в дедлайн.
    page = CatalogFilterPage(reads_until_filtered=None)
    # Короткий дедлайн: проверяется факт опроса и выхода, а не длительность.
    monkeypatch.setattr(
        create,
        "select_wizard_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, filter_timeout=0.5),
    )

    result = _run(page, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    # #836: "не найдена однозначно (совпадений: 0)" не различала опечатку и
    # исчезновение значения из каталога. При нуле совпадений сообщение теперь
    # перечисляет, что каталог реально предлагает (CatalogFilterPage без
    # фильтрации отдаёт "Аналитик" — то, что live-каталог показал бы).
    assert "не найдена в каталоге" in result.reason
    assert "Аналитик" in result.reason
    assert page.tree_reads > 1, "дерево должно перечитываться, а не читаться один раз"


class AmbiguousCatalogLocator(HydrationLocator):
    """Дерево стабильно отдаёт ДВА разных leaf с одинаковым текстом (#836)."""

    def all(self):
        self.page.tree_reads += 1
        return [
            TreeItem(AREA, "tree-selector-item-text-96", self.page),
            TreeItem(AREA, "tree-selector-item-text-97", self.page),
        ]


class AmbiguousCatalogPage(CatalogFilterPage):
    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if self.TREE in selector:
            return AmbiguousCatalogLocator(self, selector, count)
        return HydrationLocator(self, selector, count)


def test_genuine_ambiguity_is_not_confused_with_absence(monkeypatch):
    """Два разных leaf под одним текстом — отдельное сообщение о неоднозначности,
    не смешанное с "не найдена в каталоге" (#836: 0 и >1 совпадений — разные причины)."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = AmbiguousCatalogPage()
    monkeypatch.setattr(
        create,
        "select_wizard_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, filter_timeout=0.5),
    )

    result = _run(page, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена однозначно" in result.reason
    assert "совпадений: 2" in result.reason
    assert "предлагает" not in result.reason, "неоднозначность — не то же самое, что отсутствие"


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


# --- #920: единственный кандидат фильтра без точного равенства -------------


class CompositeLeafPage(HydrationRacePage):
    """Фильтр стабильно сужает дерево до единственного СОСТАВНОГО листа,
    чьё имя не равно запросу (#920, боевой прогон 2026-09-02 00:51:
    «Плотник» -> единственный лист «Столяр, плотник»)."""

    TREE = "tree-selector-item-text-"

    def __init__(self, *, leaves=None):
        super().__init__(clicks_until_hydrated=0)
        self.tree_reads = 0
        self._leaves = leaves or [TreeItem("Столяр, плотник", "tree-selector-item-text-96", self)]

    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if self.TREE in selector:
            return CompositeLeafLocator(self, selector, count)
        return HydrationLocator(self, selector, count)


class CompositeLeafLocator(HydrationLocator):
    def all(self):
        self.page.tree_reads += 1
        return self.page._leaves  # noqa: SLF001 — тестовый двойник, не production API


def _run_area(page, area, monkeypatch, *, before_click=None):
    """Боевой прогон с чужим ``area`` и коротким дедлайном опроса дерева."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    monkeypatch.setattr(
        create,
        "select_wizard_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, filter_timeout=0.5),
    )
    return create.create_resume_on_hh(
        cast(PlaywrightPage, cast(object, page)),
        area=area,
        title=TITLE,
        dry_run=False,
        before_click=before_click,
    )


def test_unique_filtered_candidate_is_accepted_without_exact_match(monkeypatch, caplog):
    """«Плотник» -> единственный лист «Столяр, плотник» — выбор, а не отказ.

    Фильтр hh.ru сузил дерево до одного листа, но строгая equality его не
    находила: имена листов составные, и команда требовала посимвольного
    ввода (боевой прогон #920). Запрос содержится в имени листа —
    однозначность по-человечески; лист кликается той же механикой, что и
    точное совпадение (клик по строке -> poll checked -> submit).
    """
    import logging

    page = CompositeLeafPage()

    with caplog.at_level(logging.INFO, logger="hhru_bot.create_resume"):
        result = _run_area(page, "Плотник", monkeypatch, before_click=lambda: None)

    assert result.success, f"единственный кандидат фильтра должен быть принят: {result.reason}"
    assert RESUME_CREATION_CATEGORY_SUBMIT in page.clicks, "каталог должен быть засабмичен"
    assert "Столяр, плотник" in caplog.text, "автопринятие обязано попасть в лог"


def test_unique_candidate_with_unexpected_role_id_refuses_before_click():
    """Фолбэк #920 не обходит гард #913: чужой role_id у кандидата — отказ.

    create-resume вызывает select_wizard_catalog_leaf без expected_role_id, но
    resume_position (direct_save) — с ним: единственный кандидат фильтра с
    id, не равным согласованному, отказывает ДО клика, как и точное
    совпадение с чужим id.
    """
    page = CompositeLeafPage()

    reason = _real_select(
        cast(PlaywrightPage, cast(object, page)),
        "Плотник",
        filter_timeout=0.5,
        expected_role_id="148",
    )

    assert "role_id=96" in reason
    assert "148" in reason
    assert page.clicks == [], "отказ до клика по строке и submit"


class DegenerateOtherPage(CompositeLeafPage):
    """Фильтр вырождается в единственный catch-all лист «Другое» (#913)."""

    def __init__(self):
        super().__init__(leaves=[TreeItem("Другое", "tree-selector-item-text-40", self)])


def test_filter_degeneration_into_other_is_refused_without_click(monkeypatch):
    """Единственный кандидат «Другое» не выбирается никогда (#913).

    Фильтр hh.ru по неточному запросу («Инженер по тестированию») сужает
    дерево до catch-all «Другое» — это отказ с именем для повтора, а не
    выбор: резюме с профессией «Другое» — молчаливая порча данных.
    """
    page = DegenerateOtherPage()

    result = _run_area(page, "Инженер по тестированию", monkeypatch, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена в каталоге" in result.reason
    assert "Другое" in result.reason
    assert "повторите" in result.reason
    assert RESUME_CREATION_CATEGORY_SUBMIT not in page.clicks, "отказ до клика"


class UnrelatedLeafPage(CompositeLeafPage):
    """Единственный кандидат фильтра не содержит запрос в своём имени."""

    def __init__(self):
        super().__init__(leaves=[TreeItem("Аналитик", "tree-selector-item-text-10", self)])


def test_unique_candidate_without_query_in_name_is_refused_with_hint(monkeypatch):
    """Единственный кандидат без запроса в имени — soft-fail с именем листа.

    «Фильтр вернул что-то одно» — ещё не однозначное совпадение: hh.ru
    фильтрует нечётко, и автопринятие неродственного листа записало бы
    профессию, которую пользователь не называл.
    """
    page = UnrelatedLeafPage()

    result = _run_area(page, "Плотник", monkeypatch, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена в каталоге" in result.reason
    assert "Аналитик" in result.reason, "имя листа для повтора должно быть в сообщении"
    assert RESUME_CREATION_CATEGORY_SUBMIT not in page.clicks, "отказ до клика"


def test_empty_area_is_never_accepted_as_substring_of_anything(monkeypatch):
    """Пустой --area не «содержится» в имени листа — автопринятия нет.

    normalize("") == \"\" — подстрока любой строки; без отдельного гварда
    пустой запрос автопринял бы первого попавшегося единственного кандидата
    (fail-closed: argparse пропускает --area "").
    """
    page = CompositeLeafPage()

    result = _run_area(page, "", monkeypatch, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert RESUME_CREATION_CATEGORY_SUBMIT not in page.clicks, "отказ до клика"


def test_multiple_candidates_without_exact_match_still_refuse(monkeypatch):
    """Несколько кандидатов фильтра — прежний отказ с перечнем (#920 гард).

    Однозначность — ровно один кандидат; два составных листа, содержащих
    запрос, разрешает только пользователь перезапуском с точным именем.
    """

    class TwoCompositeLeavesPage(CompositeLeafPage):
        def __init__(self):
            super().__init__(
                leaves=[
                    TreeItem("Столяр, плотник", "tree-selector-item-text-96", self),
                    TreeItem("Плотник-бетонщик", "tree-selector-item-text-97", self),
                ]
            )

    page = TwoCompositeLeavesPage()

    result = _run_area(page, "Плотник", monkeypatch, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена в каталоге" in result.reason
    assert "Столяр, плотник" in result.reason and "Плотник-бетонщик" in result.reason
    assert RESUME_CREATION_CATEGORY_SUBMIT not in page.clicks, "отказ до клика"


class DesyncedTreePage(CompositeLeafPage):
    """all_text_contents() и .all() стабильно расходятся (#837-семейство).

    Тексты от одного рендера, элементы — от другого: индексному
    сопоставлению доверять нельзя, значит и «единственный кандидат»
    недоказуем — автопринятие обязано молчать (fail-closed).
    """

    def locator(self, selector):
        count = 0 if selector == RESUME_LIST_CARD else 1
        if self.TREE in selector:
            return DesyncedTreeLocator(self, selector, count)
        return HydrationLocator(self, selector, count)


class DesyncedTreeLocator(HydrationLocator):
    def all(self):
        return [
            TreeItem("Столяр, плотник", "tree-selector-item-text-96", self.page),
            TreeItem("Аналитик", "tree-selector-item-text-10", self.page),
        ]

    def all_text_contents(self):
        return ["столяр, плотник"]


def test_desynced_read_disqualifies_unique_candidate(monkeypatch):
    """Рассинхрон текстов и элементов — автопринятия нет, прежний отказ.

    Единственность кандидата доказуема только на консистентном снимке
    дерева; на снимке от двух разных рендеров принимать решение — всё равно
    что сопоставить чужой текст чужому элементу (#837, тот же принцип).
    """
    page = DesyncedTreePage()

    result = _run_area(page, "Плотник", monkeypatch, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не найдена в каталоге" in result.reason
    assert RESUME_CREATION_CATEGORY_SUBMIT not in page.clicks, "отказ до клика"


def test_dry_run_never_reaches_catalog(monkeypatch):
    """Dry-run не доходит до select_wizard_catalog_leaf — кликать нечего (#920).

    Изменение семантики match не меняет dry-run план структурно: create-resume
    в dry-run возвращается сразу после подтверждения визарда, до клика по
    карточке профессии. Закрепляем, чтобы будущий рефактор не подвёл dry-run
    к каталогу.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = CompositeLeafPage()

    result = create.create_resume_on_hh(
        cast(PlaywrightPage, cast(object, page)),
        area="Плотник",
        title=TITLE,
        dry_run=True,
    )

    assert result.success
    assert "dry-run" in result.reason
    assert page.clicks == [], "dry-run не кликает ничего"
    assert page.tree_reads == 0, "dry-run не доходит до дерева каталога"


class CheckboxWrapperPage(CatalogFilterPage):
    """hh.ru оборачивает <input type=checkbox> в стилизованный контейнер.

    Живая разведка #778: ``Locator.check()`` по самому input падает с
    «Clicking the checkbox did not change its state» (у него ``tabindex="-1"``
    и оформление на родительском ``magritte-checkbox-container``), а клик по
    текстовой строке профессии переключает его штатно.
    """

    def __init__(self):
        super().__init__(reads_until_filtered=0)
        self.checked = False

    def locator(self, selector):
        if "tree-selector-input-" in selector:
            return CheckboxLocator(self, selector, 1)
        return super().locator(selector)


class CheckboxLocator(HydrationLocator):
    def check(self):
        raise PlaywrightError("Locator.check: Clicking the checkbox did not change its state")


def test_profession_is_selected_by_clicking_the_row(monkeypatch):
    """Профессия отмечается кликом по строке, а не check() по input."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = CheckboxWrapperPage()

    result = _run(page, before_click=lambda: None)

    assert result.success, f"строка профессии должна кликаться: {result.reason}"


def test_unchecked_after_row_click_refuses_to_continue(monkeypatch):
    """Клик по строке не отметил профессию — отказ до submit (fail-closed).

    Молчаливое продолжение отправило бы каталог без выбранной профессии и
    создало бы резюме не с той специализацией.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))

    class NeverChecksPage(CheckboxWrapperPage):
        def locator(self, selector):
            loc = super().locator(selector)
            if "tree-selector-input-" in selector:
                loc.is_checked = lambda: False  # type: ignore[method-assign]
            return loc

    page = NeverChecksPage()

    # Тест проверяет отказ ПОСЛЕ исчерпания дедлайна, а не сам дедлайн. С
    # реальным `_CHECKBOX_CONFIRM_TIMEOUT` (5с) и no-op `wait_for_timeout` фейка
    # цикл подтверждения крутил CPU 5 секунд wall-clock — самый долгий тест
    # сьюта. Тот же приём, что в test_checkbox_never_confirmed_* ниже:
    # детерминированные часы + короткий дедлайн.
    call_count = 0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        return call_count * 0.1

    monkeypatch.setattr(create.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        create,
        "select_wizard_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, checkbox_confirm_timeout=0.3),
    )

    result = _run(page, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не отмечена" in result.reason


class DelayedCheckLocator(HydrationLocator):
    """Чекбокс переключается React'ом АСИНХРОННО после клика по строке (#837).

    Живой замер 2026-08-30: сразу после ``Locator.click()`` на строке
    профессии ``checkbox.checked`` синхронно всё ещё ``False`` — React
    обновляет DOM только на следующем тике event loop. Статистика по 6
    успешным живым прогонам: ``checked=False`` непосредственно после клика в
    2 из 6 (~33%) случаев, при этом ``checked=True`` уже на первой же
    проверке спустя минимальную задержку — не редкий edge case.

    Детерминированно (не через реальное время): ``is_checked()`` считает
    СОБСТВЕННЫЕ обращения и возвращает ``False`` для первых
    ``reads_until_checked`` вызовов, затем ``True`` навсегда — старый код
    (один синхронный read сразу после click()) видит вызов №1 и падает
    ВСЕГДА при ``reads_until_checked >= 1``; новый код (polling до
    подтверждённого состояния) переживает произвольное число ложных ``False``
    и проходит ВСЕГДА, пока дедлайн не исчерпан.

    Счётчик читок живёт на ``page``, а не на этом locator'е: боевой код
    вызывает ``page.locator(checkbox_selector)`` ДВАЖДЫ на один и тот же
    чекбокс (сначала для ``wait_for(state="visible")``, затем через
    ``_one()``) — каждый вызов создаёт свежий объект locator, поэтому
    "сколько раз уже спросили is_checked()" обязано пережить пересоздание
    locator'а, тот же паттерн, что уже применяется в ``CatalogLocator`` и
    ``StaleSnapshotLocator`` выше.
    """

    def __init__(self, page, selector, count):
        super().__init__(page, selector, count)

    def is_checked(self):
        self.page.checkbox_reads += 1
        return self.page.checkbox_reads > self.page.reads_until_checked


class DelayedCheckPage(CatalogFilterPage):
    """Строка каталога кликается штатно; чекбокс отмечается с задержкой."""

    def __init__(self, *, reads_until_checked):
        super().__init__(reads_until_filtered=0)
        self.reads_until_checked = reads_until_checked
        self.checkbox_reads = 0

    def locator(self, selector):
        if "tree-selector-input-" in selector:
            return DelayedCheckLocator(self, selector, 1)
        return super().locator(selector)


def test_checkbox_race_after_row_click_is_not_reported_as_unchecked(monkeypatch):
    """#837: клик отметил строку, но React обновляет checked асинхронно —
    один немедленный синхронный read ловит гонку и ложно фейлит валидный
    выбор. Polling до подтверждённого состояния обязан пережить это."""
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    page = DelayedCheckPage(reads_until_checked=3)

    result = _run(page, before_click=lambda: None)

    assert result.success, f"гонка чекбокса не должна фейлить валидный выбор: {result.reason}"


def test_permanently_unchecked_checkbox_still_refuses_to_continue(monkeypatch):
    """Контроль: чекбокс, который НИКОГДА не переключается, — честный failed.

    Polling за подтверждённым состоянием не должен превращать fail-closed
    #837 в бесконечный опрос или в молчаливое продолжение без реально
    выбранной профессии — как только polling-дедлайн исчерпан, это тот же
    отказ, что и в test_unchecked_after_row_click_refuses_to_continue.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    # Мок page.wait_for_timeout() — no-op (не спит реально), поэтому цикл
    # опроса без монотонных часов, привязанных к количеству итераций, мог бы
    # сделать сколько угодно проверок за доли секунды реального времени —
    # число итераций и скорость машины не должны решать исход теста.
    # Подменяем time.monotonic() детерминированной последовательностью: она
    # растёт на фиксированный шаг с КАЖДЫМ вызовом (polling читает её дважды
    # за итерацию — для проверки дедлайна и, до и после цикла, снаружи), так
    # что дедлайн гарантированно исчерпывается после конечного, заранее
    # известного числа итераций, независимо от скорости CPU.
    call_count = 0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        return call_count * 0.1

    monkeypatch.setattr(create.time, "monotonic", fake_monotonic)
    page = DelayedCheckPage(reads_until_checked=1_000_000)
    monkeypatch.setattr(
        create,
        "select_wizard_catalog_leaf",
        lambda p, area, **_: _real_select(p, area, checkbox_confirm_timeout=0.3),
    )

    result = _run(page, before_click=lambda: None)

    assert not result.success
    assert not result.uncertain, "выбор профессии — до точки невозврата"
    assert "не отмечена" in result.reason


def test_resume_id_is_read_from_wizard_query_url(monkeypatch):
    """После сохранения hh.ru ведёт на следующий шаг визарда, id — в query.

    Боевой прогон 2026-08-29: резюме создано, но код ждал путь
    ``/resume/{id}`` и упирался в таймаут на
    ``/profile/resume/educations?resume={id}&hhtmFrom=...`` — исход
    ``uncertain`` при фактическом успехе.
    """
    monkeypatch.setattr(create, "goto_hh", lambda page, url: page.goto(url))
    new_id = "3805d2e4ff11065aaa0039ed1f554f657a6b41"

    class WizardNextStepPage(CheckboxWrapperPage):
        def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
            if not isinstance(url, str):
                self.url = (
                    f"https://hh.ru/profile/resume/educations?resume={new_id}"
                    "&hhtmFrom=my_resumes&hhtmFromLabel=create_resume_header"
                )
            return None

    page = WizardNextStepPage()

    result = _run(page, before_click=lambda: None)

    assert result.success, f"id из query должен распознаваться: {result.reason}"
    assert result.new_resume_id == new_id
