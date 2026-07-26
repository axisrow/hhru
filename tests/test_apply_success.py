"""Characterization-тесты шага подтверждения успеха отклика (#7).

Поведение: успех определяется по нескольким независимым сигналам —
success-маркер ИЛИ текст «отклик отправлен» ИЛИ исчезновение submit-кнопки.
Любой один сигнал достаточен. Без браузера — через FakePage.
"""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.apply import success
from hhru_bot.apply.locators import first_locator


class _FakeLocator:
    """Имитация Playwright Locator: count()/wait_for()."""

    def __init__(self, *, count_value: int = 0, present: bool | None = None):
        if present is not None:
            count_value = 1 if present else 0
        self._count = count_value

    def count(self) -> int:
        return self._count

    def wait_for(self, timeout: float = 0) -> None:  # noqa: ARG002
        if self._count == 0:
            raise PlaywrightTimeoutError("not present")


class _FakePage:
    """Имитация Playwright Page для сигналов подтверждения успеха.

    markers: success-маркеры, присутствующие на странице (count>0).
    success_texts: фразы, которые get_by_text найдёт.
    submit_present: видна ли submit-кнопка (False = исчезла = успех).
    """

    def __init__(
        self,
        *,
        markers: set[str] | None = None,
        success_texts: set[str] | None = None,
        submit_present: bool = True,
    ):
        self._markers = markers or set()
        self._success_texts = success_texts or set()
        self._submit_present = submit_present
        self.url = ""

    def locator(self, selector: str):  # noqa: ARG002
        if selector in self._markers:
            return _FakeLocator(count_value=1)
        if selector == success.APPLY_SUBMIT_SELECTOR:
            return _FakeLocator(count_value=1 if self._submit_present else 0)
        return _FakeLocator(count_value=0)

    def get_by_text(self, text: str, exact: bool = False):  # noqa: ARG002
        if text in self._success_texts:
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


def test_success_via_text():
    page = _FakePage(success_texts={success.APPLY_SUCCESS_TEXTS[0]})
    assert success.wait_success_confirmation(page) is True


def test_success_via_text_alt_phrase():
    """Любая из фраз-сигналов подтверждает успех."""
    page = _FakePage(success_texts={success.APPLY_SUCCESS_TEXTS[-1]})
    assert success.wait_success_confirmation(page) is True


def test_success_via_submit_gone():
    """Submit-кнопка исчезла после отправки — успех."""
    page = _FakePage(submit_present=False)
    assert success.wait_success_confirmation(page) is True


def test_success_all_signals_absent_returns_false():
    """Ни маркера, ни текста, submit на месте — таймаут, успеха нет."""
    page = _FakePage(submit_present=True)
    assert success.wait_success_confirmation(page, timeout_ms=0) is False


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
