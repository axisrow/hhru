"""Characterization-тесты bump.py: гонка рендера disabled-hint (#139).

Без браузера — через FakePage, имитирующий минимальный Playwright API.
Главный регрессионный сценарий: hint «поднимать ещё рано» появляется в DOM
С ЗАДЕРЖКОЙ (не сразу после goto), а не мгновенно. Старый код читал
``disabled_hint.count() > 0`` сразу после ``goto_hh`` — не успевшая
отрисоваться подсказка давала 0 → код шёл дальше и жал кнопку поднятия
(обход кулдауна hh.ru, необратимое действие на живом аккаунте).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.bump as bump_module
from hhru_bot.bump import bump_resume
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.selector_groups import resume_page

pytestmark = pytest.mark.integration


class _FakeLocator:
    """Один «элемент».

    ``count()`` — снимок DOM В МОМЕНТ ВЫЗОВА, без ожидания (моделирует
    непрогрузившийся рендер: пока страница не «дорисовалась», элемента ещё
    нет в DOM). ``wait_for`` — явное ожидание: если элемент появляется до
    ``rendered_after`` вызовов (или сразу, если ``rendered_after is None``
    и ``present=True``), считается, что он «дождался» и не кидает исключение;
    иначе таймаут. Это позволяет тесту отличить «прочитали count() сразу без
    ожидания → гонка» от «дождались wait_for → корректно».
    """

    def __init__(
        self,
        present: bool,
        click_log: list[str] | None = None,
        name: str = "",
        *,
        render_delayed: bool = False,
        wait_error: bool = False,
        click_error: bool = False,
    ):
        self._present = present
        self._click_log = click_log
        self._name = name
        # render_delayed=True: count() сразу после goto (без ожидания) лжёт — 0,
        # хотя элемент в итоге появится. wait_for обязан дождаться и увидеть True.
        self._render_delayed = render_delayed
        # wait_error=True: cycle-review #139 — не-timeout PlaywrightError
        # (strict-mode violation и т.п.), аномалия, а не легитимное отсутствие.
        self._wait_error = wait_error
        # click_error=True: #176 — PlaywrightError в момент click() (клик мог
        # уйти на hh.ru, но ожидание после клика упало).
        self._click_error = click_error

    def count(self) -> int:
        if self._render_delayed:
            # Немедленное чтение без ожидания — застаёт непрогрузившийся DOM.
            return 0
        return 1 if self._present else 0

    def wait_for(self, *, timeout: float = 0, state: str = "visible") -> None:  # noqa: ARG002
        # wait_for моделирует реальный рендер: дожидается финального состояния,
        # а не снимка на момент вызова.
        if self._wait_error:
            raise PlaywrightError(f"runtime error waiting for {self._name}")
        if not self._present:
            raise PlaywrightTimeoutError(f"{self._name} not visible")

    def click(
        self,
        *,
        timeout: float | None = None,
        force: bool | None = None,
        no_wait_after: bool | None = None,
    ) -> None:  # noqa: ARG002
        if self._click_error:
            # #176: клик «выполняется», но Playwright падает — как navigation
            # timeout/target closed уже после отправки действия на hh.ru.
            raise PlaywrightError(f"click on {self._name} failed after dispatch")
        if self._click_log is not None:
            self._click_log.append(self._name)


class FakeBumpPage:
    """Имитация Page для bump_resume. hint/button присутствие настраивается отдельно."""

    def __init__(
        self,
        *,
        hint_present: bool,
        button_present: bool = True,
        hint_render_delayed: bool = False,
        hint_wait_error: bool = False,
        button_click_error: bool = False,
    ):
        self.goto_calls: list[str] = []
        self.click_log: list[str] = []
        self._hint_present = hint_present
        self._button_present = button_present
        self._hint_render_delayed = hint_render_delayed
        self._hint_wait_error = hint_wait_error
        self._button_click_error = button_click_error

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str):
        if selector == resume_page.RESUME_BUMP_DISABLED_HINT:
            return _FakeLocator(
                self._hint_present,
                self.click_log,
                "hint",
                render_delayed=self._hint_render_delayed,
                wait_error=self._hint_wait_error,
            )
        if selector == resume_page.RESUME_BUMP_BUTTON:
            return _FakeLocator(
                self._button_present,
                self.click_log,
                "button",
                click_error=self._button_click_error,
            )
        return _FakeLocator(False)


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/abc123",
        search=SearchFilters(text="python", area=1),
        cover_letter=None,
    )


def test_bump_hint_present_blocks_click():
    """hint виден сразу — bump не жмётся, причина отказа возвращается."""
    page = FakeBumpPage(hint_present=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "рано" in result.reason
    assert page.click_log == []
    assert result.acted is False  # #163: клика не было — без паузы и записи


def test_bump_placeholder_url_does_not_navigate():
    page = FakeBumpPage(hint_present=False)
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/XXXXXXXXXXXXXXXXXXXXXXXX",
        search=SearchFilters(text="python", area=1),
    )

    result = bump_resume(page, resume, dry_run=False)

    assert result.success is False
    assert "плейсхолдер" in result.reason
    assert page.goto_calls == []
    assert result.acted is False  # #163: отсев до навигации — hh.ru не тронут


def test_bump_delayed_hint_still_blocks_click():
    """РЕГРЕССИЯ #139: hint появляется не мгновенно (гонка рендера) — на момент
    немедленного ``count()`` его в DOM ещё нет, но он в итоге отрисуется.
    bump обязан ЖДАТЬ (wait_for), а не читать count() сразу, и НЕ нажать кнопку.

    Старый код (``disabled_hint.count() > 0`` сразу после goto) не ждал,
    видел 0 совпадений на непрогрузившемся DOM и жал кнопку поднятия в обход
    кулдауна hh.ru.
    """
    page = FakeBumpPage(hint_present=True, hint_render_delayed=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "рано" in result.reason
    assert "button" not in page.click_log


def test_bump_no_hint_clicks_button():
    """Hint отсутствует (детерминированно, после ожидания) — кнопка поднятия жмётся."""
    page = FakeBumpPage(hint_present=False, button_present=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is True
    assert page.click_log == ["button"]
    assert result.acted is True  # #163: реальный клик — пауза обязательна


def test_bump_dry_run_does_not_click_even_without_hint():
    page = FakeBumpPage(hint_present=False, button_present=True)

    result = bump_resume(page, _resume(), dry_run=True)

    assert result.success is True
    assert page.click_log == []
    assert result.acted is False  # #163: симуляция без клика — без паузы


def test_bump_hint_wait_error_is_fail_closed_not_traceback():
    """cycle-review #139: не-timeout ошибка при ожидании hint (аномалия
    страницы, не легитимное отсутствие) — fail-closed BumpResult, а не
    непойманный traceback и не тихий переход к клику по кнопке."""
    page = FakeBumpPage(hint_present=True, button_present=True, hint_wait_error=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "button" not in page.click_log


def test_bump_no_button_found_fails():
    page = FakeBumpPage(hint_present=False, button_present=False)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "не найдена" in result.reason


def test_bump_login_form_is_checked_after_navigation(monkeypatch):
    page = FakeBumpPage(hint_present=False)
    events: list[str] = []

    def fake_goto(p, url, **_kwargs):
        events.append("goto")
        p.goto(url)

    def fake_has_login_form(_page):
        events.append("auth")
        assert events == ["goto", "auth"]
        return True

    monkeypatch.setattr(bump_module, "goto_hh", fake_goto)
    monkeypatch.setattr(bump_module, "has_login_form", fake_has_login_form)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "Сессия недействительна" in result.reason
    assert events == ["goto", "auth"]


def test_bump_click_error_is_uncertain_acted_not_traceback():
    """#176: Playwright упал в момент клика поднятия (клик мог уйти на hh.ru).
    Раньше исключение пробрасывалось наружу: bump_resume не возвращал BumpResult,
    командный цикл валился трейсбеком ДО record_action/throttle.wait — поднятие
    происходило, но история/кулдаун 4ч о нём не узнали бы. Fail-closed: возвращаем
    acted+uncertain, команда по ним пишет action 'uncertain' и ждёт паузу."""
    page = FakeBumpPage(hint_present=False, button_present=True, button_click_error=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "неопределён" in result.reason
