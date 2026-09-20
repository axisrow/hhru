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
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD_LINK_PREFIX

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

    @property
    def first(self) -> _FakeLocator:
        # close_stale_contacts_alert (#1189) адресует элементы через .first.
        return self

    def is_visible(self) -> bool:
        # #1189: close_stale_contacts_alert читает видимость попапа через .first.
        return self._present

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

    def or_(self, other: _FakeLocator) -> _FakeOrLocator:
        # #1188: пост-клик вердикт ждёт «hint ИЛИ кнопка» через locator.or_.
        return _FakeOrLocator(self, other)

    def click(
        self,
        *,
        timeout: float | None = None,
        force: bool | None = None,
        no_wait_after: bool | None = None,
    ) -> None:  # noqa: ARG002
        # Лог клика пишется и при click_error: пост-клик вердикт (#1188) читает
        # состояние страницы ПОСЛЕ сбоя, фейк должен знать, что клик был.
        if self._click_log is not None:
            self._click_log.append(self._name)
        if self._click_error:
            # #176: клик «выполняется», но Playwright падает — как navigation
            # timeout/target closed уже после отправки действия на hh.ru.
            raise PlaywrightError(f"click on {self._name} failed after dispatch")


class _FakeOrLocator:
    """locator.or_(...) для пост-клик вердикта #1188: виден, если виден хоть
    один из двух сигналов кулдауна (hint или активная кнопка)."""

    def __init__(self, left: _FakeLocator, right: _FakeLocator):
        self._left = left
        self._right = right

    @property
    def first(self) -> _FakeOrLocator:
        return self

    def wait_for(self, *, timeout: float = 0, state: str = "visible") -> None:  # noqa: ARG002
        if not (self._left._present or self._right._present):
            raise PlaywrightTimeoutError("neither hint nor bump button visible")

    def is_visible(self) -> bool:
        return self._left._present or self._right._present


class _FakeCard:
    """Карточка резюме div[data-qa='resume']: скоуп для hint/кнопки поднятия."""

    def __init__(self, page: FakeBumpPage):
        self._page = page

    def locator(self, selector: str):
        if selector == resume_page.RESUME_BUMP_DISABLED_HINT:
            # #1188: hint, появляющийся ТОЛЬКО после сбоя клика (поднятие
            # ушло, карточка перерисовалась) — виден пост-клик вердикту,
            # но не pre-click чеку кулдауна.
            present = self._page._hint_present or (
                self._page._hint_appears_after_click and "button" in self._page.click_log
            )
            return _FakeLocator(
                present,
                self._page.click_log,
                "hint",
                render_delayed=self._page._hint_render_delayed,
                wait_error=self._page._hint_wait_error,
            )
        if selector == resume_page.RESUME_BUMP_BUTTON:
            # #1188: кнопка, исчезающая после сбоя клика (карточка
            # перерисовалась, состояние кулдауна не подтверждается).
            present = self._page._button_present and not (
                self._page._button_gone_after_click_error and "button" in self._page.click_log
            )
            return _FakeLocator(
                present,
                self._page.click_log,
                "button",
                click_error=self._page._button_click_error,
            )
        return _FakeLocator(False)


class _FakeCardLink:
    """Якорь карточки resume-card-link-<id>: .first.wait_for + ancestor-резолв."""

    def __init__(self, page: FakeBumpPage):
        self._page = page

    @property
    def first(self) -> _FakeCardLink:
        return self

    def wait_for(self, *, state: str = "visible", timeout: float = 0) -> None:  # noqa: ARG002
        # #1076: wait_for — единственный «ход времени» фейка: render_after_waits
        # = N означает, что список/карточка отрисовываются В ТЕЧЕНИЕ N-го
        # ожидания (оно всё равно таймаутится — ожидающий успел проверить до
        # рендера); последующие ожидания видят элемент. count() время НЕ
        # двигает — снимок DOM на момент вызова.
        self._page._wait_calls += 1
        if not self._page._card_visible:
            raise PlaywrightTimeoutError("resume card not visible")

    def locator(self, selector: str):
        # bump_resume поднимается от якоря к div[data-qa='resume'] предком.
        assert selector.startswith("xpath=ancestor::"), selector
        return _FakeCard(self._page)


class _FakeAnyCardLink:
    """Префиксный локатор «хоть какая-то карточка списка» (#1076)."""

    def __init__(self, page: FakeBumpPage):
        self._page = page

    @property
    def first(self) -> _FakeAnyCardLink:
        return self

    def count(self) -> int:
        if self._page._ssr_anchors:
            return 1
        return 1 if self._page._list_visible else 0

    def wait_for(self, *, state: str = "visible", timeout: float = 0) -> None:  # noqa: ARG002
        self._page._wait_calls += 1
        if not self._page._list_visible:
            raise PlaywrightTimeoutError("resume list not rendered")


class FakeBumpPage:
    """Имитация Page для bump_resume (поток 2026-09-08: список резюме).

    Навигация идёт на /applicant/resumes; карточка резюме резолвится якорем
    resume-card-link-<id>, hint/кнопка — внутри карточки.

    #1076: ``render_after_waits`` — сколько wait_for должны пройти «впустую»
    (таймаут), прежде чем список/карточка «отрисуются» (моделирует lazy-render
    позже первого бюджета ожидания). ``other_cards_present`` — есть ли в
    списке ЧУЖИЕ карточки, когда нашей нет (гидрация vs реальное отсутствие).
    """

    def __init__(
        self,
        *,
        hint_present: bool,
        button_present: bool = True,
        card_present: bool = True,
        hint_render_delayed: bool = False,
        hint_wait_error: bool = False,
        button_click_error: bool = False,
        render_after_waits: int = 0,
        other_cards_present: bool = True,
        ssr_anchors: bool = False,
        stale_alert_present: bool = False,
        stale_cancel_present: bool | None = None,
        hint_appears_after_click: bool = False,
        button_gone_after_click_error: bool = False,
    ):
        self.goto_calls: list[str] = []
        self.click_log: list[str] = []
        self.card_link_selectors: list[str] = []
        self._card_present = card_present
        self._hint_present = hint_present
        self._button_present = button_present
        self._hint_render_delayed = hint_render_delayed
        self._hint_wait_error = hint_wait_error
        self._button_click_error = button_click_error
        # #1188: пост-клик состояние карточки для вердикта после сбоя клика.
        self._hint_appears_after_click = hint_appears_after_click
        self._button_gone_after_click_error = button_gone_after_click_error
        self._render_after_waits = render_after_waits
        self._other_cards_present = other_cards_present
        # #1076 (ревью PR #1079): SSR-якоря списка есть в DOM ещё до
        # гидрации — count() > 0 при не-видимом списке.
        self._ssr_anchors = ssr_anchors
        # #1189: модалка «Контакты в резюме могли устареть» смонтирована
        # и видима при заходе на список (живой census 2026-09-20).
        self._stale_alert_present = stale_alert_present
        # None — cancel живёт тем же флагом, что и модалка; False отдельно —
        # для кейса «модалка видима, а dismiss-кнопки нет» (cycle-review #1190).
        self._stale_cancel_present = (
            stale_alert_present if stale_cancel_present is None else stale_cancel_present
        )
        self._wait_calls = 0

    @property
    def _rendered(self) -> bool:
        return self._wait_calls >= self._render_after_waits

    @property
    def _list_visible(self) -> bool:
        return self._rendered and (self._card_present or self._other_cards_present)

    @property
    def _card_visible(self) -> bool:
        return self._rendered and self._card_present

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str):
        if selector.startswith("a[data-qa='resume-card-link-"):
            self.card_link_selectors.append(selector)
            return _FakeCardLink(self)
        if selector == RESUME_LIST_CARD_LINK_PREFIX:
            return _FakeAnyCardLink(self)
        if selector == resume_page.STALE_CONTACTS_SYNC_ALERT:
            return _FakeLocator(self._stale_alert_present, self.click_log, "stale-alert")
        if selector == resume_page.STALE_CONTACTS_SYNC_ALERT_CANCEL:
            return _FakeLocator(self._stale_cancel_present, self.click_log, "stale-alert-cancel")
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
    командный цикл валился трейсбеком ДО record_action/throttle.wait. Fail-closed:
    возвращаем acted+uncertain, команда по ним пишет action 'uncertain' и ждёт
    паузу. #1188: uncertain остаётся только когда пост-клик вердикт НЕ смог
    подтвердить состояние кулдауна (кнопка исчезла, hint не появился); когда
    состояние читается — вердикт определённый (см. тесты ниже)."""
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        button_click_error=True,
        button_gone_after_click_error=True,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "неопределён" in result.reason


# --- #1188: пост-клик вердикт по состоянию кулдауна после сбоя клика ----------


def test_bump_click_error_button_active_is_certain_failed():
    """#1188 (суть #1161): клик упал, но карточка после сбоя подтверждена —
    кнопка поднятия активна, hint кулдауна нет. hh.ru перехваченный клик не
    видел, поднятия не было: определённый failed вместо uncertain — кулдаун
    4ч больше не съедает действие за прогон (uncertain в last_action_at
    держал повтор до старения строки)."""
    page = FakeBumpPage(hint_present=False, button_present=True, button_click_error=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.acted is True
    assert result.uncertain is False
    assert "кнопка поднятия активна" in result.reason
    assert "неопределён" not in result.reason


def test_bump_click_error_hint_appeared_is_confirmed_success():
    """#1188: клик упал (navigation/target closed после диспетча), но hint
    кулдауна после сбоя отрисован — hh.ru поднятие засчитал: success вместо
    потерянного uncertain, acted=True (реальное действие было)."""
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        button_click_error=True,
        hint_appears_after_click=True,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is True
    assert result.acted is True
    assert result.uncertain is False
    assert "поднятие подтверждено" in result.reason


def test_bump_click_error_with_stale_alert_and_unreadable_state_is_uncertain():
    """#1188 fallback: модалка видима при сбое и закрыта dismiss-кнопкой, но
    состояние кулдауна после сбоя НЕ подтвердилось (кнопка исчезла, hint не
    появился) — прежний fail-closed uncertain (#176), reason называет и
    модалку, и непрочитанное состояние. Прямой тест ветки bump.py:276-285."""
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        stale_alert_present=True,
        button_click_error=True,
        button_gone_after_click_error=True,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "не подтвердилось" in result.reason
    assert "модалка" in result.reason
    assert "stale-alert-cancel" in page.click_log


def test_bump_click_error_with_stale_alert_certain_failed_names_it():
    """#1188 + #1189: перехват модалкой контактов при активной кнопке после
    сбоя — определённый failed, reason называет и модалку (закрыта dismiss-кнопкой,
    замены контактов не было), и активную кнопку; модалка не съедает ни этот
    вердикт, ни следующий прогон."""
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        stale_alert_present=True,
        button_click_error=True,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.acted is True
    assert result.uncertain is False
    assert "модалка" in result.reason
    assert "кнопка поднятия активна" in result.reason
    assert "stale-alert-cancel" in page.click_log


# --- поток 2026-09-08: кнопка поднятия на СПИСКЕ резюме -----------------------


def test_bump_navigates_to_resumes_list_not_resume_page():
    """Живой факт 2026-09-08: кнопка мигрировала со страницы резюме на
    /applicant/resumes — поток обязан открывать список, не /resume/<id>."""
    page = FakeBumpPage(hint_present=False, button_present=True)

    bump_resume(page, _resume(), dry_run=False)

    assert page.goto_calls == [bump_module.RESUMES_LIST_URL]


def test_bump_card_missing_refuses_without_click():
    """Резюме нет в списке (удалено/недоступно) — внятный отказ, не таймаут
    на поиске кнопки; преемник banner-чека #972 для списочного потока."""
    page = FakeBumpPage(hint_present=False, button_present=True, card_present=False)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "не найдено в списке" in result.reason
    assert page.click_log == []
    assert result.acted is False


# --- #1076: query-суффикс в resume_url и медленная гидрация списка ----------


def test_bump_query_suffix_in_resume_url_still_finds_card():
    """#1076 (живой прогон 2026-09-09): resume_url, скопированный из браузера
    с query-суффиксом (?source=…), раньше давал resume_id вида ``<hash>?…``,
    точный матч data-qa не совпадал и живое резюме вечно репортилось
    «удалено». Нормализация — в config._resume_id_from_url (единый источник
    для селекторов/истории/edit-роутов), bump видит уже чистый id."""
    page = FakeBumpPage(hint_present=False, button_present=True)
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/abc123?source=employer_page",
        search=SearchFilters(text="python", area=1),
    )

    result = bump_resume(page, resume, dry_run=True)

    assert result.success is True
    # Селектор построен по чистому идентификатору, без query.
    assert page.card_link_selectors == ["a[data-qa='resume-card-link-abc123']"]


def test_bump_fragment_suffix_in_resume_url_is_stripped():
    """#1076: fragment-суффикс (#…) в resume_url отрезается так же, как query."""
    page = FakeBumpPage(hint_present=False, button_present=True)
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/abc123#experience",
        search=SearchFilters(text="python", area=1),
    )

    result = bump_resume(page, resume, dry_run=True)

    assert result.success is True
    assert page.card_link_selectors == ["a[data-qa='resume-card-link-abc123']"]


def test_bump_slow_hydration_late_card_still_bumps():
    """#1076: карточка отрисовывается ПОЗЖЕ первого бюджета ожидания
    (lazy-render, #858) — раньше это «удалено», теперь гидрация-гейт даёт
    списку второй бюджет, карточка дожидается и bump идёт дальше."""
    # render_after_waits=2: 1-й wait (карточка) таймаутится; 2-й wait
    # (появление списка) застывает отрисовку; повторный wait карточки — ок.
    page = FakeBumpPage(hint_present=False, button_present=True, render_after_waits=2)

    result = bump_resume(page, _resume(), dry_run=True)

    assert result.success is True
    assert "удалено" not in result.reason


def test_bump_list_never_renders_is_indeterminate_not_deleted():
    """#1076: список так и не отрисовался (сеть/анти-бот) — честное «наличие
    резюме не подтверждено», НЕ ложное «удалено»; acted=False (#163)."""
    # render_after_waits=10: оба бюджета ожидания истекают впустую.
    page = FakeBumpPage(
        hint_present=False, button_present=True, card_present=False, render_after_waits=10
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "не подтверждено" in result.reason
    assert "удалено" not in result.reason
    assert result.acted is False
    assert page.click_log == []


def test_bump_real_absence_with_other_cards_is_refusal():
    """#1076: список отрисован (чужие карточки есть), нашей нет —
    подтверждённое отсутствие → отказ «не найдено в списке (удалено…)»."""
    page = FakeBumpPage(
        hint_present=False, button_present=True, card_present=False, other_cards_present=True
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "не найдено в списке" in result.reason
    assert result.acted is False


def test_bump_ssr_anchors_pre_hydration_do_not_falsely_delete():
    """#1076 (ревью PR #1079): SSR-разметка содержит чужие якоря списка ещё
    до гидрации (count() > 0 при не-видимом списке), наш якорь появляется
    только после клиентского рендера. Вердикт по count() дал бы ложное
    «удалено» в окне SSR→гидрация; вердикт — только по ВИДИМОМУ списку и
    повторному поиску карточки."""
    # SSR-якоря в DOM сразу; карточка нашего резюме отрисовывается на
    # 2-м ожидании (в течение ожидания видимости списка).
    page = FakeBumpPage(
        hint_present=False, button_present=True, render_after_waits=2, ssr_anchors=True
    )

    result = bump_resume(page, _resume(), dry_run=True)

    assert result.success is True
    assert "удалено" not in result.reason


def test_bump_ssr_anchors_list_never_visible_is_indeterminate():
    """#1076 (nit-ревью PR #1079): SSR-якоря в DOM есть, но список так и не
    стал ВИДИМЫМ — состояние страницы не подтверждено (fail-closed #5),
    НЕ «удалено»: таймаут видимости списка и таймаут повторного поиска
    карточки — разные вердикты и не смешиваются в одном except."""
    # render_after_waits=10: все ожидания истекают; count()>0 за счёт
    # SSR-якорей, видимости нет.
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        card_present=False,
        render_after_waits=10,
        ssr_anchors=True,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert "не подтверждено" in result.reason
    assert "удалено" not in result.reason
    assert result.acted is False


# --- #1189: модалка «Контакты в резюме могли устареть» ------------------------


def test_bump_closes_stale_contacts_alert_before_click():
    """#1189 (боевой сбой #1161): модалка смонтирована при заходе на список
    резюме и перехватывает клик поднятия. Обязана закрыться dismiss-кнопкой
    «Закрыть» ДО клика — тогда успех вместо uncertain и кулдауна 4ч; пометка
    в reason делает срабатывание видимым."""
    page = FakeBumpPage(hint_present=False, button_present=True, stale_alert_present=True)

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is True
    assert page.click_log == ["stale-alert-cancel", "button"]
    assert "модалка контактов закрыта" in result.reason


def test_bump_stale_alert_without_visible_cancel_has_no_false_close_mark():
    """cycle-review #1190: True close обязан означать ФАКТ клика. Модалка видима,
    но dismiss-кнопка нет (аномальный DOM) — клика не было, пометка «закрыта
    кликом» в success-reason не появляется."""
    page = FakeBumpPage(
        hint_present=False,
        button_present=True,
        stale_alert_present=True,
        stale_cancel_present=False,
    )

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is True
    assert page.click_log == ["button"]
    assert result.reason == "success"


def test_bump_stale_alert_not_touched_in_dry_run():
    # dry-run: ноль мутаций страницы — попап не закрывается (выход до боевого блока).
    page = FakeBumpPage(hint_present=False, button_present=True, stale_alert_present=True)

    result = bump_resume(page, _resume(), dry_run=True)

    assert result.success is True
    assert page.click_log == []
