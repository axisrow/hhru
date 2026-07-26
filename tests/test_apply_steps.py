"""Characterization-тесты apply/steps: явные ожидания Playwright (#6).

Без браузера — через FakePage, имитирующий минимальный Playwright API, который
использует steps.py: locator(...).wait_for(state='visible', timeout=...),
click(), fill(), expect_navigation(). Страхуют поведение wait'ов: time.sleep
убран, опциональные поля определяются ловом PlaywrightTimeoutError, обязательный
submit даёт отказ при отсутствии.
"""

from __future__ import annotations

import contextlib

from playwright.sync_api import Error
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.apply import steps
from hhru_bot.selector_groups import apply_form, vacancy_page


class _FakeLocator:
    """Один «элемент»: visible=True → wait_for проходит, False → PlaywrightTimeoutError.

    Записывает вызовы click/fill, чтобы тесты проверяли, какие поля реально трогались.
    """

    def __init__(self, selector: str, state: _SelectorState) -> None:
        self.selector = selector
        self._state = state

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:  # noqa: ARG002
        # Моделируем реальное поведение Playwright: в strict mode при >1 совпадении
        # wait_for кидает обычный Error (не PlaywrightTimeoutError) — это и есть
        # регрессия, которую ловит test_fill_form_resume_select_multiple_matches.
        if self._state.match_count > 1:
            raise Error(  # noqa: TRY002 — имитация playwright._impl._errors.Error
                f"strict mode violation: {self.selector} resolved to "
                f"{self._state.match_count} elements"
            )
        if not self._state.visible:
            raise PlaywrightTimeoutError(f"{self.selector} not visible")

    def click(self) -> None:
        self._state.clicks += 1

    def fill(self, value: str) -> None:
        self._state.fills.append(value)

    def count(self) -> int:
        return 1 if self._state.visible else 0

    def get_attribute(self, _name: str) -> str | None:
        return None

    def nth(self, _i: int) -> _FakeLocator:
        return self


class _SelectorState:
    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        # match_count>1 имитирует строгий режим Playwright: селектор совпал с
        # несколькими элементами (коллекция резюме/несколько кнопок).
        self.match_count = 1
        self.clicks = 0
        self.fills: list[str] = []


class FakeStepsPage:
    """Страница с независимо настраиваемым состоянием каждого селектора."""

    def __init__(self) -> None:
        self.states: dict[str, _SelectorState] = {}
        self.navigation_entered = 0

    def _state(self, selector: str) -> _SelectorState:
        return self.states.setdefault(selector, _SelectorState())

    def set_visible(self, selector: str, visible: bool = True) -> _SelectorState:
        st = self._state(selector)
        st.visible = visible
        return st

    def set_match_count(self, selector: str, count: int) -> _SelectorState:
        """Селектор совпал с `count` элементами → имитация strict-mode нарушения."""
        st = self._state(selector)
        st.match_count = count
        st.visible = count > 0
        return st

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, self._state(selector))

    @contextlib.contextmanager
    def expect_navigation(self, **_kwargs):
        self.navigation_entered += 1
        yield


# --- wait_apply_button ---


def test_wait_apply_button_visible_returns_true():
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    assert steps.wait_apply_button(page) is True


def test_wait_apply_button_missing_returns_false():
    # Кнопка не «появилась» → wait_for кидает PlaywrightTimeoutError → False.
    page = FakeStepsPage()
    assert steps.wait_apply_button(page) is False


# --- navigate_to_response_form ---


def test_navigate_clicks_inside_expect_navigation_and_waits_submit():
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    # Клик по apply-кнопке + вход в expect_navigation.
    assert page.navigation_entered == 1
    assert page._state(vacancy_page.VACANCY_APPLY_BUTTON).clicks == 1


def test_navigate_does_not_raise_when_form_never_renders():
    # Форма (submit) не отрисовалась — ждём таймаут, логируем, но НЕ падаем.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    # submit намеренно отсутствует

    steps.navigate_to_response_form(page)  # не должен бросать

    assert page.navigation_entered == 1


# --- fill_response_form: только обязательный submit ---


def test_fill_form_only_submit_present_clicks_submit_returns_none():
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    submit = page._state(apply_form.APPLY_SUBMIT_BUTTON)
    assert submit.clicks == 1
    # Опциональные поля не трогались.
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 0
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == []
    assert page._state(apply_form.APPLY_RESUME_SELECT).clicks == 0


def test_fill_form_missing_submit_returns_reason_no_click():
    page = FakeStepsPage()
    # Никаких полей, включая submit.

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "кнопка отправки отклика не найдена" in result


# --- fill_response_form: опциональные поля ---


def test_fill_form_with_letter_fills_textarea():
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_COVER_LETTER_TOGGLE, True)
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "тело письма")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 1
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == ["тело письма"]
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_letter_toggle_absent_skips_textarea():
    # Toggle отсутствует → его не кличем; textarea тоже отсутствует → не заполняем.
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 0
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == []


def test_fill_form_resume_select_multiple_matches_does_not_crash():
    # Регрессия #6: APPLY_RESUME_SELECT по дизайну коллекция (несколько резюме).
    # Playwright wait_for в strict mode при >1 совпадении кидает обычный Error
    # (НЕ PlaywrightTimeoutError) — _is_visible должен это пережить (трактовать как
    # «поле есть, но не однозначное») и НЕ ронять apply-run необработанным исключением.
    page = FakeStepsPage()
    page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    # apply-run не прерван исключением; submit нажат.
    assert result is None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


# --- константы ---


def test_optional_field_timeout_is_short():
    # Опциональные поля ждут недолго: отсутствие — это норма, не долгоиграющая ошибка.
    assert steps.OPTIONAL_FIELD_TIMEOUT_MS < steps.APPLY_TIMEOUT_MS
