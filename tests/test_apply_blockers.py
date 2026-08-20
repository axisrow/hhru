"""Fixture-level tests for HH post-click response blockers."""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.apply.blockers import (
    PostClickBlocker,
    PostSubmitLimitExceeded,
    handle_post_click_blockers,
    raise_if_post_submit_limit,
)
from hhru_bot.history import SKIP_REASONS
from hhru_bot.selector_groups import vacancy_page

pytestmark = pytest.mark.integration


def _blocker_ctx(*, verifier):
    """Минимальный ApplyContext для проверки финализации блокеров."""
    from hhru_bot.apply.pipeline import ApplyContext
    from hhru_bot.search import VacancyCard

    return ApplyContext(
        page=_Page(),
        vacancy=VacancyCard("1", "Вакансия", "Компания", "https://hh.ru/vacancy/1"),
        resume_id="RID",
        cover_letter_template="письмо",
        dry_run=False,
        verifier=verifier,
    )


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return self.page.elements.get(self.selector, (False, ""))[0]

    def inner_text(self) -> str:
        return self.page.elements.get(self.selector, (False, ""))[1]

    def wait_for(self, *, state: str = "visible", timeout: float = 0) -> None:
        self.page.waited.append(self.selector)
        self.page.wait_timeouts.append(timeout)
        # Совокупный селектор ждёт первый видимый якорь; в фикстурах DOM
        # синхронный, поэтому достаточно проверить любую из частей.
        if not any(
            self.page.elements.get(part.strip(), (False, ""))[0]
            for part in self.selector.split(",")
        ):
            raise PlaywrightError("locator.wait_for: Timeout exceeded")

    def click(self) -> None:
        self.page.clicked.append(self.selector)
        self.page.elements[self.selector] = (False, self.inner_text())


class _Page:
    def __init__(self, *elements: tuple[str, str]) -> None:
        self.elements = {selector: (True, text) for selector, text in elements}
        self.clicked: list[str] = []
        self.waited: list[str] = []
        self.wait_timeouts: list[float] = []

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)


def test_relocation_is_skipped_by_default_without_click():
    page = _Page((vacancy_page.VACANCY_RELOCATION_CONFIRM, "Готовы к переезду?"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.RELOCATION_NOT_ALLOWED
    assert page.clicked == []


def test_relocation_can_be_confirmed_only_by_explicit_policy():
    page = _Page((vacancy_page.VACANCY_RELOCATION_CONFIRM, "Готовы к переезду?"))

    result = handle_post_click_blockers(page, allow_relocation=True)

    assert result is None
    assert page.clicked == [vacancy_page.VACANCY_RELOCATION_CONFIRM]


def test_similar_popup_is_closed_without_becoming_terminal():
    page = _Page((vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE, "Не сейчас"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is None
    assert page.clicked == [vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE]


def test_direct_application_requires_alert_text():
    page = _Page(
        (vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL, "Отменить"),
        (vacancy_page.VACANCY_DIRECT_APPLICATION_ALERT, "Вакансия с прямым откликом"),
    )

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.DIRECT_APPLICATION


def test_limit_stops_run_before_submit():
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.kind == "limit_exceeded"
    assert result.stop_run is True


def test_limit_is_checked_again_after_submit():
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    with pytest.raises(PostSubmitLimitExceeded):
        raise_if_post_submit_limit(page)


def test_response_warning_is_a_terminal_skip():
    page = _Page((vacancy_page.VACANCY_RESPONSE_REJECT_WARNING, "Скорее всего, будет отказ"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.RESPONSE_REJECTED


def test_blockers_wait_for_render_before_strict_checks():
    # CLAUDE.md п.4: без явного ожидания проверка читает ещё не отрисованный
    # React-DOM и систематически не видит модалку.
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    handle_post_click_blockers(page, allow_relocation=False)

    assert page.waited, "терминальные проверки выполнены без ожидания рендера"


def test_post_navigation_blocker_recovers_a_response_that_actually_went_out():
    # #207: после навигации отклик мог уйти, а модалка показаться поверх.
    # Без внешней проверки такой исход молча стал бы skip и потерял отклик.
    from hhru_bot.apply.pipeline import _finalize_blocker
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    ctx = _blocker_ctx(verifier=lambda *_: NegotiationsVerifyResult("found", "topic=42"))
    blocker = PostClickBlocker(
        "response_rejected",
        "HH.ru показал предупреждение",
        SKIP_REASONS.RESPONSE_REJECTED,
        post_navigation=True,
    )

    result = _finalize_blocker(ctx, blocker)

    assert result.success is True
    assert result.acted is True
    assert result.skipped is False


def test_post_navigation_blocker_keeps_its_verdict_when_no_response_found():
    from hhru_bot.apply.pipeline import _finalize_blocker
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    ctx = _blocker_ctx(verifier=lambda *_: NegotiationsVerifyResult("not_found", "нет карточки"))
    blocker = PostClickBlocker(
        "direct_application",
        "отклик на сайте работодателя",
        SKIP_REASONS.DIRECT_APPLICATION,
        post_navigation=True,
    )

    result = _finalize_blocker(ctx, blocker)

    assert result.success is False
    assert result.skip_reason == SKIP_REASONS.DIRECT_APPLICATION


def test_pre_navigation_blocker_does_not_call_the_verifier():
    # До навигации отклик физически невозможен — лишний поход в negotiations
    # был бы чистой тратой запроса на каждую такую вакансию.
    from hhru_bot.apply.pipeline import _finalize_blocker

    calls = []

    def verifier(*args):
        calls.append(args)
        raise AssertionError("verifier must not be called before navigation")

    ctx = _blocker_ctx(verifier=verifier)
    blocker = PostClickBlocker(
        "relocation_not_allowed",
        "переезд не разрешён",
        SKIP_REASONS.RELOCATION_NOT_ALLOWED,
    )

    result = _finalize_blocker(ctx, blocker)

    assert calls == []
    assert result.skip_reason == SKIP_REASONS.RELOCATION_NOT_ALLOWED


def test_second_pass_does_not_wait_at_all():
    # Штатный путь без модалки не должен ждать render-таймаут дважды.
    # ВАЖНО: пропуск выражается отсутствием вызова wait_for, а НЕ timeout=0 —
    # в Playwright 0 означает «таймаут отключён», то есть бесконечное ожидание.
    page = _Page()

    handle_post_click_blockers(
        page, allow_relocation=False, render_timeout_ms=0, post_navigation=True
    )

    assert page.wait_timeouts == []


def test_render_wait_never_passes_zero_timeout_to_playwright():
    # Страж от зависания: 0 в Playwright = ждать вечно.
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    handle_post_click_blockers(page, allow_relocation=False)

    assert page.wait_timeouts and all(t > 0 for t in page.wait_timeouts)


def test_account_limit_stops_the_run_even_when_the_response_went_out():
    # Лимит откликов — свойство аккаунта: подтверждённый отклик его не отменяет.
    # Без этого прогон продолжил бы долбиться в исчерпанный лимит.
    from hhru_bot.apply.pipeline import _finalize_blocker
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    ctx = _blocker_ctx(verifier=lambda *_: NegotiationsVerifyResult("found", "topic=42"))
    blocker = PostClickBlocker(
        "limit_exceeded",
        "лимит откликов исчерпан",
        stop_run=True,
        post_navigation=True,
    )

    result = _finalize_blocker(ctx, blocker)

    assert result.success is True
    assert result.stop_run is True


def test_account_limit_stops_the_run_when_verification_is_unavailable():
    from hhru_bot.apply.pipeline import _finalize_blocker
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    ctx = _blocker_ctx(
        verifier=lambda *_: NegotiationsVerifyResult("indeterminate", "список не прочитан")
    )
    blocker = PostClickBlocker(
        "limit_exceeded",
        "лимит откликов исчерпан",
        stop_run=True,
        post_navigation=True,
    )

    result = _finalize_blocker(ctx, blocker)

    assert result.stop_run is True
    assert result.uncertain is True
