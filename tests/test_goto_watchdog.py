"""#1130: wall-clock watchdog вокруг goto и кап verify-фазы.

Driver-side таймеры Playwright — не жёсткая гарантия (инцидент 2026-09-13),
поэтому блокированный вызов прерывается SIGALRM-предохранителем с фатальной
классификацией GotoWatchdogTimeout, а verify-фаза вакансии в pipeline ограничена
wall-clock капом и финализируется fail-closed uncertain.
"""

import signal
import threading
import time

import pytest

from hhru_bot.apply import pipeline as apply_pipeline
from hhru_bot.apply.pipeline import ApplyContext
from hhru_bot.browser import GotoWatchdogTimeout, goto_hh, wall_clock_guard
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


class _SleepingGotoPage:
    """Фейковый Page: goto зависает дольше любого тестового бюджета."""

    def __init__(self) -> None:
        self.goto_calls = 0

    def goto(self, url, *, wait_until=None, timeout=None):  # noqa: ANN001, ARG002
        self.goto_calls += 1
        time.sleep(5.0)


class _QuietPage(_SleepingGotoPage):
    """Фейковый Page: зависающий goto + пустая страница для анти-бот детектора."""

    url = ""

    def locator(self, selector):  # noqa: ANN001, ARG002
        return _QuietLocator()


class _QuietLocator:
    def filter(self, **_kwargs):  # noqa: ANN002
        return self

    def count(self) -> int:
        return 0


def test_watchdog_interrupts_hung_goto_without_retry(monkeypatch):
    """Watchdog режет зависший goto; фатальный исход не ретраится goto_hh."""
    monkeypatch.setattr("hhru_bot.browser._GOTO_WALL_CLOCK_SECONDS", 0.3)
    page = _SleepingGotoPage()
    with pytest.raises(GotoWatchdogTimeout):
        goto_hh(page, "https://hh.ru/search/vacancy")
    assert page.goto_calls == 1


def test_watchdog_is_fatal_even_when_response_observed(monkeypatch):
    """GotoWatchdogTimeout не переквалифицируется в ThrottledChannelDetected."""
    monkeypatch.setattr("hhru_bot.browser._GOTO_WALL_CLOCK_SECONDS", 0.3)

    class _ThrottledLikePage(_SleepingGotoPage):
        # Наличие .on/.remove_listener включает response-слушатель goto_hh,
        # но response_observed остаётся False — проверяем, что даже при
        # PlaywrightTimeoutError-подобном сценарии watchdog фатален.
        def on(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        def remove_listener(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    with pytest.raises(GotoWatchdogTimeout):
        goto_hh(_ThrottledLikePage(), "https://hh.ru/search/vacancy")


def test_wall_clock_guard_restores_timer_and_handler():
    def _body() -> None:
        with wall_clock_guard(30.0):
            pass

    _body()
    # Ничего не протекло наружу: таймер сброшен, обработчик дефолтный.
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is signal.SIG_DFL


def test_nested_guard_does_not_touch_outer_timer():
    """Cycle-review PR #1136: вложенный guard не переустанавливает и не
    ре-армит внешний таймер — ре-арм микросекундного остатка выстреливал
    после выхода из всех guard'ов (RuntimeError в CI linux/py3.12)."""
    with wall_clock_guard(1.0):
        with wall_clock_guard(30.0):
            time.sleep(0.3)
        # Таймер внешний, не тронут внутренним: осталось ~0.7с реального времени.
        left, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL
    assert 0.3 < left < 0.85
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is signal.SIG_DFL


def test_nested_guard_is_clamped_by_outer_budget():
    """Внутренний бюджет не продлевает внешний кап (#1130: кап verify > goto)."""
    started = time.monotonic()
    with pytest.raises(GotoWatchdogTimeout):
        with wall_clock_guard(0.3):
            with wall_clock_guard(30.0):
                time.sleep(5.0)
    assert time.monotonic() - started < 2.0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is signal.SIG_DFL


def test_guard_outside_main_thread_is_noop():
    """Вне главного потока guard не может прерывать — но и не ломает работу."""
    result = {}

    def _run() -> None:
        try:
            with wall_clock_guard(0.2):
                time.sleep(0.05)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=10)
    assert result == {"ok": True}


def test_verify_phase_cap_is_fatal_and_propagates(monkeypatch):
    """#1130 + review PR #1136: зависшая verify-фаза прерывается капом;
    GotoWatchdogTimeout фатален — пробрасывается из финализатора в
    _execute_apply_wave (финализирует uncertain и останавливает прогон),
    а не глотается общим fail-closed обработчиком с продолжением прогона
    на недостоверном драйвере."""
    monkeypatch.setattr(apply_pipeline, "VERIFY_PHASE_CAP_SECONDS", 0.3)

    def _hung_verifier(page, vacancy_id, resume_id):  # noqa: ANN001, ARG002
        goto_hh(page, "https://hh.ru/applicant/negotiations")
        raise AssertionError("не должен дойти до скана")

    ctx = ApplyContext(
        page=_QuietPage(),
        vacancy=VacancyCard(vacancy_id="136370736", title="x", company="y", url="u"),
        resume_id="00001",
        cover_letter_template="p",
        dry_run=False,
        acted=True,
        verifier=_hung_verifier,
    )
    with pytest.raises(GotoWatchdogTimeout):
        apply_pipeline._finalize_post_click_failure(ctx, "navigate timeout")


def test_guard_does_not_touch_monotonic_clock(monkeypatch):
    """CI linux/py3.12 (PR #1136): тесты патчат time.monotonic конечными
    итераторами; вызов monotonic в guard'е давал StopIteration внутри
    генератора contextlib → RuntimeError "generator raised StopIteration".
    Горячий путь goto_hh не должен зависеть от монотонных часов."""

    class _InstantPage:
        def goto(self, url, *, wait_until=None):  # noqa: ANN001, ARG002
            return None

    monkeypatch.setattr(time, "monotonic", iter([]).__next__)
    goto_hh(_InstantPage(), "https://hh.ru/search/vacancy")


def test_nested_guard_asserts_shorter_than_outer():
    """Review PR #1136: вложенный бюджет короче внешнего капа — громкий
    AssertionError, а не молчаливая потеря внутреннего бюджета."""
    with pytest.raises(AssertionError):
        with wall_clock_guard(1.0):
            with wall_clock_guard(0.2):
                pass
