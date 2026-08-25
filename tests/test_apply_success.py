"""Characterization-тесты шага подтверждения успеха отклика (#7).

Поведение: успех определяется по ПОЗИТИВНЫМ сигналам — success-маркер
(CSS-цепочка) ИЛИ текст «отклик отправлен» (регистронезависимо). Сигналы
опрашиваются в цикле до таймаута (покрывают асинхронный рендер fallback-маркера
или текста). Отрицательный признак (исчезновение submit) успехом НЕ считается.
Без браузера — через FakePage.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.apply import success
from hhru_bot.apply.locators import first_locator

pytestmark = pytest.mark.integration


def test_default_confirmation_timeout_is_short_and_randomized(monkeypatch):
    """Локальный UI — быстрый путь перед авторитетной внешней проверкой."""
    chosen: list[tuple[int, int]] = []

    def _randint(lower: int, upper: int) -> int:
        chosen.append((lower, upper))
        return lower

    monkeypatch.setattr(success.random, "randint", _randint)
    page = _FakePage(markers={success.APPLY_SUCCESS_MARKER})

    assert success.wait_success_confirmation(page) is True
    assert chosen == [
        (
            success.SUCCESS_CONFIRMATION_MIN_TIMEOUT_MS,
            success.SUCCESS_CONFIRMATION_MAX_TIMEOUT_MS,
        )
    ]


class _FakeLocator:
    """Имитация Playwright Locator: count()/wait_for().

    appear_after: если задано, count() возвращает 0, пока page._probe_count не
    превысит appear_after (моделирует асинхронный рендер сигнала после submit).
    """

    @property
    def first(self):
        return self

    def __init__(
        self,
        *,
        count_value: int = 0,
        present: bool | None = None,
        appear_after: int | None = None,
        page: _FakePage | None = None,
    ):
        if present is not None:
            count_value = 1 if present else 0
        self._count = count_value
        self._appear_after = appear_after
        self._page = page

    def count(self) -> int:
        if self._page is not None:
            self._page._probe_count += 1
        if self._appear_after is not None and self._page is not None:
            return 1 if self._page._probe_count > self._appear_after else 0
        return self._count

    def wait_for(self, *, timeout: float = 0) -> None:  # noqa: ARG002
        if self.count() == 0:
            raise PlaywrightTimeoutError("not present")


class _FakePage:
    """Имитация Playwright Page для сигналов подтверждения успеха.

    markers: success-маркеры, присутствующие сразу (count>0).
    late_markers: селекторы, появляющиеся после late_after опросов count()
        (моделирует async-рендер fallback-сигнала после submit).
    success_texts: фразы, которые get_by_text найдёт (сопоставляются с regex).
    late_success_text: фраза, появляющаяся после late_after опросов.
    submit_present: фиктивный флаг (success.py submit больше не использует).
    """

    def __init__(
        self,
        *,
        markers: set[str] | None = None,
        late_markers: set[str] | None = None,
        success_texts: set[str] | None = None,
        late_success_text: str | None = None,
        late_after: int = 1,
        submit_present: bool = True,
    ):
        self._markers = markers or set()
        self._late_markers = late_markers or set()
        self._success_texts = success_texts or set()
        self._late_success_text = late_success_text
        self._late_after = late_after
        self._submit_present = submit_present  # noqa: ARG002 — фиктивный
        self._probe_count = 0
        self.url = ""

    def wait_for_timeout(self, _ms: float) -> None:  # noqa: ARG002 — мгновенный no-op в тестах
        return None

    def locator(self, selector: str):  # noqa: ARG002
        if selector in self._late_markers:
            return _FakeLocator(appear_after=self._late_after, page=self)
        if selector in self._markers:
            return _FakeLocator(count_value=1)
        return _FakeLocator(count_value=0)

    def get_by_text(self, text, *, exact: bool = False):  # noqa: ARG002
        # Playwright get_by_text принимает str | Pattern. Для Pattern ищем
        # по множеству заготовленных фраз (как делает реальный regex-поиск).
        import re

        pattern = re.compile(text) if isinstance(text, str) else text
        if self._late_success_text is not None and pattern.search(self._late_success_text):
            return _FakeLocator(appear_after=self._late_after, page=self)
        for phrase in self._success_texts:
            if pattern.search(phrase):
                return _FakeLocator(count_value=1)
        return _FakeLocator(count_value=0)


# --- multi-signal success ---


def test_success_via_marker():
    page = _FakePage(markers={success.APPLY_SUCCESS_MARKER})
    assert success.wait_success_confirmation(page) is True


def test_success_via_fallback_marker():
    """Второй success-маркер из цепочки тоже подтверждает успех."""
    page = _FakePage(markers={success.APPLY_SUCCESS_MARKERS[-1]})
    assert success.wait_success_confirmation(page) is True


def test_success_via_current_magritte_marker():
    """The current response popup exposes the Magritte success attachment."""
    page = _FakePage(markers={"[data-qa='responded-success-attach-cover-letter']"})
    assert success.wait_success_confirmation(page) is True


def test_success_via_text():
    page = _FakePage(success_texts={"Отклик отправлен"})
    assert success.wait_success_confirmation(page) is True


def test_success_via_text_alt_phrase():
    """Любая из фраз-сигналов подтверждает успех."""
    page = _FakePage(success_texts={"Вы откликнулись на вакансию"})
    assert success.wait_success_confirmation(page) is True


def test_success_via_text_case_insensitive():
    """FIX2 (#7): текст-сигнал регистронезависим (re.I).

    Ишью #7 явно требует get_by_text(re.compile('отклик отправлен', re.I)).
    Любой регистр/пунктуация фразы подтверждает успех.
    """
    page = _FakePage(success_texts={"ОТКЛИК ОТПРАВЛЕН."})
    assert success.wait_success_confirmation(page) is True


def test_success_via_text_lowercase():
    page = _FakePage(success_texts={"ваш отклик отправлен на вакансию"})
    assert success.wait_success_confirmation(page) is True


def test_success_via_late_fallback_marker():
    """Cycle-3 fix: fallback-маркер отрендерился асинхронно после one-shot опроса.

    Основной маркер отсутствует; fallback-маркер из цепочки появляется после
    первого опроса. Poll-loop должен поймать его в пределах timeout, а не
    ждать только основной маркер и не вернуть False (что дало бы status=failed
    — без дедупликации и без счёта в лимит → повторные попытки сверх лимита).
    """
    page = _FakePage(
        late_markers={success.APPLY_SUCCESS_MARKERS[-1]},
        late_after=1,
    )
    assert success.wait_success_confirmation(page, timeout_ms=2000) is True


def test_success_via_late_text():
    """Cycle-3 fix: текст-признак отрендерился асинхронно после one-shot опроса."""
    page = _FakePage(late_success_text="Отклик отправлен", late_after=1)
    assert success.wait_success_confirmation(page, timeout_ms=2000) is True


def test_submit_gone_alone_is_not_success():
    """Cycle-2 fix: исчезновение submit САМО ПО СЕБОЕ — НЕ успех.

    Отрицательный признак (отсутствие submit) удовлетворяется не только
    успехом, но и auth-redirect / CAPTCHA / ошибкой валидации / throttle /
    maintenance — исчерпать перечень для непроверенной вёрстки hh.ru нельзя.
    False success пишется в историю (status='success'), has_applied навсегда
    исключает вакансию, сгорает дневной лимит. Поэтому успех подтверждается
    только ПОЗИТИВНЫМИ сигналами (маркер / текст); submit-gone не источник True.
    """
    page = _FakePage(submit_present=False)
    page.url = "https://hh.ru/applicant/vacancy_response?vacancyId=42"
    assert success.wait_success_confirmation(page, timeout_ms=0) is False


def test_submit_gone_on_auth_url_still_not_success():
    """Submit исчез + URL логина — по-прежнему НЕ успех (раньше гвардил FIX1)."""
    page = _FakePage(submit_present=False)
    page.url = "https://hh.ru/account/login?back=/applicant/vacancy_response"
    assert success.wait_success_confirmation(page, timeout_ms=0) is False


def test_submit_gone_with_marker_is_success():
    """Маркер присутствует (позитивный сигнал) — успех, submit тут ни при чём."""
    page = _FakePage(markers={success.APPLY_SUCCESS_MARKER}, submit_present=False)
    assert success.wait_success_confirmation(page) is True


def test_submit_gone_with_text_is_success():
    """Текст-признак (позитивный сигнал) — успех, submit тут ни при чём."""
    page = _FakePage(success_texts={"Отклик отправлен"}, submit_present=False)
    assert success.wait_success_confirmation(page) is True


def test_success_all_signals_absent_returns_false():
    """Ни маркера, ни текста, submit на месте — таймаут, успеха нет."""
    page = _FakePage(submit_present=True)
    assert success.wait_success_confirmation(page, timeout_ms=0) is False


def test_terminal_check_interrupts_post_submit_poll():
    page = _FakePage()

    def _terminal() -> None:
        raise RuntimeError("confirmed challenge")

    with pytest.raises(RuntimeError, match="confirmed challenge"):
        success.wait_success_confirmation(page, terminal_check=_terminal)


def test_success_timeout_logs_page_url(caplog):
    """Ишью #7 критерий готовности: ветки ошибок логируют URL.

    На таймауте (ни один сигнал не сработал) предупреждение должно содержать
    page.url — для диагностики первого живого прогона.
    """
    import logging

    page = _FakePage(submit_present=True)
    page.url = "https://hh.ru/applicant/vacancy_response?vacancyId=42"

    with caplog.at_level(logging.WARNING, logger="hhru_bot.apply.success"):
        result = success.wait_success_confirmation(page, timeout_ms=0)

    assert result is False
    assert any(
        "https://hh.ru/applicant/vacancy_response?vacancyId=42" in rec.message
        for rec in caplog.records
    )


# --- first_locator ---


def test_first_locator_picks_first_present():
    page = _FakePage(markers={"b"})
    loc = first_locator(page, "a", "b", "c")
    assert loc is not None
    assert loc.count() == 1


def test_first_locator_none_when_all_absent():
    page = _FakePage()
    assert first_locator(page, "a", "b") is None


def test_first_locator_empty_selectors():
    page = _FakePage()
    assert first_locator(page) is None


def test_first_locator_priority_order():
    """Если есть несколько — возвращается первый по порядку селекторов."""
    page = _FakePage(markers={"a", "c"})
    loc = first_locator(page, "a", "c")
    assert loc is not None
    assert loc.count() == 1
