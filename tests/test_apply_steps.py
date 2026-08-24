"""Characterization-тесты apply/steps: явные ожидания Playwright (#6).

Без браузера — через FakePage, имитирующий минимальный Playwright API, который
использует steps.py: locator(...).wait_for(state='visible', timeout=...),
click(), fill(). Страхуют поведение wait'ов: time.sleep
убран, опциональные поля определяются ловом PlaywrightTimeoutError, обязательный
submit даёт отказ при отсутствии.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Error, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.apply import steps
from hhru_bot.selector_groups import apply_form, vacancy_page

pytestmark = pytest.mark.integration


class _FakeLocator:
    """Один «элемент»: visible=True → wait_for проходит, False → PlaywrightTimeoutError.

    Записывает вызовы click/fill, чтобы тесты проверяли, какие поля реально трогались.
    """

    @property
    def first(self):
        # В реальном Playwright .first снимает strict mode: wait_for на коллекции
        # через .first проходит (ждёт первый совпавший), а на всей коллекции без .first
        # кидает strict-mode Error. Возвращаем локатор с _strict=False.
        loc = _FakeLocator(
            self.selector,
            self._state,
            strict=False,
            resume_title_inner=self._resume_title_inner,
            resume_toggle=self._resume_toggle,
        )
        # Делегирование or_ переживает .first — реальный Playwright тоже
        # сохраняет, на какой элемент действует локатор.
        loc._delegate_to = self._delegate_to
        return loc

    def __init__(
        self,
        selector: str,
        state: _SelectorState,
        *,
        strict: bool = True,
        option_resume_id: str | None = None,
        resume_title_inner: bool = False,
        resume_toggle: bool = False,
    ) -> None:
        self.selector = selector
        self._state = state
        self._strict = strict
        # option_resume_id: селектор — [data-qa='magritte-select-option-{id}'],
        # адресует конкретную опцию по resume_id напрямую (живой DOM —
        # опция несёт identity в самом data-qa, а не в href, которого на форме нет).
        self._option_resume_id = option_resume_id
        self._resume_title_inner = resume_title_inner
        self._resume_toggle = resume_toggle
        # or_-локатор: состояние, на которое реально действуют click()/fill()
        # (видимый операнд). None — обычный локатор, действует на своё состояние.
        self._delegate_to: _SelectorState | None = None

    def _live_option_ids(self) -> list[str]:
        # Текущие «живые» resume_id опций. option-локатор моделирует «момент клика» —
        # после открытия dropdown, поэтому при заданном reorder всегда отражает
        # post-scan DOM (тот же принцип, что раньше был у href, TOCTOU-тесты сохранены).
        ids = self._state.option_resume_ids
        if self._option_resume_id is not None and self._state.reorder_to:
            ids = self._state.reorder_to
        return ids

    def wait_for(self, *, state: str = "visible", timeout: float = 0) -> None:
        self._state.wait_for_timeout = timeout
        if self._state.wait_error:
            # Cycle-5: имитация не-timeout PlaywrightError (runtime/selector failure).
            raise Error(f"runtime error waiting for {self.selector}")
        if state == "attached":
            # Триггер (APPLY_RESUME_SELECT) — единственный элемент, ждётся как attached
            # перед открытием dropdown; не коллекция.
            if not self._state.visible:
                raise PlaywrightTimeoutError(f"{self.selector} not attached")
            return
        if state == "hidden":
            # Живой DOM (probe-дампы 2026-08-20): панель drop-base появляется по
            # клику на триггер и НЕ закрывается сама после выбора опции — её
            # закрывает повторный клик по триггеру. Поэтому «скрыта» = панель
            # не открыта. dropdown_stays_open имитирует случай, когда закрыть
            # не удалось и панель перекрыла бы submit.
            if self._state.dropdown_stays_open:
                raise PlaywrightTimeoutError(f"{self.selector} still visible")
            if self._state.dropdown_opened:
                raise PlaywrightTimeoutError(f"{self.selector} still visible")
            return
        if self._option_resume_id is not None:
            # живой DOM: опция резюме — data-qa-локатор, видимость появляется после клика
            # по триггеру (React-рендер dropdown), не сразу. disappear_after_wait НЕ
            # проверяется здесь — это TOCTOU для count() ПОСЛЕ успешного wait_for
            # (тот же принцип, что раньше был у триггера #340).
            matches = [i for i in self._live_option_ids() if i == self._option_resume_id]
            if len(matches) == 0:
                raise PlaywrightTimeoutError(f"{self.selector} not visible")
            return
        if not self._state.visible:
            raise PlaywrightTimeoutError(f"{self.selector} not visible")

    def click(self, *, timeout=None, no_wait_after=None) -> None:
        # Фиксируем kwargs клика — регрессия #80: клик apply-кнопки, триггерящий
        # навигацию, должен идти с no_wait_after=True (ожидание навигации владеет
        # внешний 90с expect_navigation, а не внутренний 30с action-timeout клика).
        # Только src/-вызываемые параметры (timeout, no_wait_after) — реальный
        # Playwright.click() принимает больше, но в src/hhru_bot их никто не
        # передаёт (#409 simplification review).
        passed = {"timeout": timeout, "no_wait_after": no_wait_after}
        self._state.click_kwargs.append({k: v for k, v in passed.items() if v is not None})
        # #176: имитация Playwright-исключения в момент клика (navigation timeout
        # после POST, target closed) — действие могло уйти на hh.ru.
        if self._state.click_error is not None:
            raise self._state.click_error
        if self._state.trigger_click_error is not None and self._option_resume_id is None:
            raise self._state.trigger_click_error
        if self._option_resume_id is not None:
            # Strict data-qa-локатор: ровно одна живая опция с этим resume_id,
            # иначе Error (как реальный Playwright strict mode при != 1 совпадении).
            matches = [i for i in self._live_option_ids() if i == self._option_resume_id]
            if len(matches) != 1:
                raise Error(  # noqa: TRY002
                    f"strict mode violation: {self.selector} resolved to {len(matches)} "
                    f"elements (resume_id={self._option_resume_id!r})"
                )
            self._state.selected_resume_id = matches[0]
            if self._state.dropdown_auto_closes:
                self._state.dropdown_opened = False
        else:
            if self._resume_toggle:
                # Live DOM: resume-title is nested inside div[role="button"];
                # the ancestor owns the close behavior.
                self._state.dropdown_opened = False
                self._state.resume_toggle_clicks += 1
            elif self._resume_title_inner:
                # A repeated click on the inner div does not close drop-base.
                self._state.dropdown_opened = True
                self._state.resume_title_clicks += 1
            else:
                # The first click opens the panel. This generic behavior keeps
                # the fake useful for unrelated trigger selectors.
                self._state.dropdown_opened = not self._state.dropdown_opened
        if self._delegate_to is not None:
            # or_-локатор: клик засчитывается видимому операнду (реальный
            # Playwright кликает по фактически совпавшему элементу).
            self._delegate_to.clicks += 1
        else:
            self._state.clicks += 1

    def fill(self, value: str) -> None:
        target = self._delegate_to if self._delegate_to is not None else self._state
        target.fills.append(value)

    def count(self) -> int:
        # Для опции резюме (option_resume_id) — число живых совпадений по resume_id.
        # disappear_after_wait: TOCTOU — опция была видна в wait_for, но пропала
        # к моменту финальной проверки count() (transient re-render/drift).
        if self._option_resume_id is not None:
            if self._state.disappear_after_wait:
                return 0
            matches = [i for i in self._live_option_ids() if i == self._option_resume_id]
            return len(matches)
        return 1 if self._state.visible else 0

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() объединяет apply-button и
        # already-responded-маркеры одним локатором. Фейк комбинирует state:
        # visible/match_count по OR, чтобы wait_for/count отражали объединение.
        #
        # Письмо в модалке: тоггл/textarea тоже адресуются через or_ по двум
        # shape (модалка vs полная форма). Реальный Playwright click()/fill()
        # на or_-локаторе действует на тот элемент, который реально совпал,
        # поэтому клики и заполнения ДОЛЖНЫ долетать до состояния видимого
        # операнда, а не в комбинированную копию (иначе тест не увидит,
        # что письмо заполнено). Приоритет — первый видимый операнд.
        target = self._state if self._state.visible else other._state
        combined = _SelectorState(visible=self._state.visible or other._state.visible)
        combined.is_collection = self._state.is_collection or other._state.is_collection
        combined.match_count = max(self._state.match_count, other._state.match_count)
        combined.wait_error = self._state.wait_error or other._state.wait_error
        combined.dropdown_stays_open = (
            self._state.dropdown_stays_open or other._state.dropdown_stays_open
        )
        loc = _FakeLocator(f"({self.selector})|({other.selector})", combined, strict=False)
        loc._delegate_to = target
        return loc


class _SelectorState:
    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        # is_collection=True имитирует коллекцию (несколько резюме): wait_for кидает
        # strict-mode Error, count() возвращает match_count.
        self.is_collection = False
        self.match_count = 1
        self.clicks = 0
        self.click_kwargs: list[dict] = []
        self.fills: list[str] = []
        # Имитация TOCTOU: селектор был видим в wait_for, но исчез к count().
        # Моделирует transient re-render/drift между двумя вызовами Playwright.
        self.disappear_after_wait = False
        # живой DOM: resume_id опций резюме — то, что видно в момент открытия dropdown
        # (сразу после клика по триггеру). Заменяет старые option_hrefs — опция
        # теперь адресуется по resume_id прямо в data-qa, href на форме нет вовсе.
        self.option_resume_ids: list[str] = []
        # Имитация reorder-TOCTOU (cycle-3, сохранена под новую модель): DOM
        # «меняется» между открытием dropdown и кликом по опции — reorder_to
        # отражает live-состояние в момент click()/wait_for() опции.
        self.reorder_to: list[str] | None = None
        # Имитация не-timeout PlaywrightError в wait_for (cycle-5): runtime/selector
        # failure, который НЕ должен маскироваться под «выбора нет».
        self.wait_error = False
        # #176: PlaywrightError в момент click() — клик мог уйти (POST отправлен,
        # но ожидание после клика упало). None = обычный успешный клик.
        self.click_error: Exception | None = None
        # живой DOM: PlaywrightError в момент клика по триггеру резюме (открытие
        # dropdown) — отдельно от click_error (submit) и от опций.
        self.trigger_click_error: Exception | None = None
        # живой DOM: какой resume_id реально был кликнут (заменяет current_href).
        self.selected_resume_id: str | None = None
        self.dropdown_opened = False
        # Current live behavior: selecting an option closes drop-base. Set False
        # to model the older/stuck shape that needs the toggle fallback.
        self.dropdown_auto_closes = True
        self.resume_title_clicks = 0
        self.resume_toggle_clicks = 0
        # Боевой случай 2026-08-20: после клика по опции React не закрыл dropdown,
        # раскрытый listbox перекрыл submit-кнопку → Locator.click ретраил 30с
        # (`subtree intercepts pointer events`) и падал в SubmitClickUncertain.
        # True → wait_for(state="hidden") на опции не дождётся закрытия.
        self.dropdown_stays_open = False
        # #442 cycle-review (F6): последний timeout, переданный в wait_for() —
        # проверяет, что вызывающий контролирует ожидание submit-кнопки через
        # form_timeout_ms, а не через jitter/другой параметр.
        self.wait_for_timeout: float | None = None


class FakeStepsPage:
    """Страница с независимо настраиваемым состоянием каждого селектора."""

    def __init__(self) -> None:
        self.states: dict[str, _SelectorState] = {}
        self.navigation_entered = 0
        self.last_navigation_timeout: int | None = None
        self.last_navigation_wait_until: str | None = None
        # #179: wait_for_url заменил expect_navigation. wait_for_url_error=None —
        # успешный кейс (URL сменился); задать PlaywrightTimeoutError — имитация
        # same-document/SPA-навигации, где URL меняется, но lifecycle-событие
        # документа не наступает (тест регрессии на этот баг ниже).
        self.wait_for_url_calls: list[tuple[str, str | None, int | None]] = []
        self.wait_for_url_error: Exception | None = None
        self.screenshot_calls = 0
        self.content_calls = 0
        self.wait_for_function_calls: list[tuple[str, str | None, int | None]] = []

    def screenshot(self, *, full_page: bool | None = None, path=None) -> bytes:
        self.screenshot_calls += 1
        return b"png"

    def content(self) -> str:
        self.content_calls += 1
        return "<html>diagnostic</html>"

    def wait_for_function(self, _expression, *, arg=None, timeout=None, polling=None):
        self.wait_for_function_calls.append(("function", arg, timeout))
        if not self._state(arg).visible:
            raise PlaywrightTimeoutError("condition not met")

    def _state(self, selector: str) -> _SelectorState:
        return self.states.setdefault(selector, _SelectorState())

    def set_visible(self, selector: str, visible: bool = True) -> _SelectorState:
        st = self._state(selector)
        st.visible = visible
        return st

    def set_match_count(self, selector: str, count: int) -> _SelectorState:
        """Селектор совпал с `count` элементами → коллекция (имитация strict-mode)."""
        st = self._state(selector)
        st.is_collection = True
        st.match_count = count
        st.visible = count > 0
        return st

    def locator(self, selector: str) -> _FakeLocator:
        # живой DOM: опция резюме — [data-qa='magritte-select-option-{resume_id}'],
        # адресует конкретный resume_id напрямую (не href, которого нет на форме).
        # Резолвится к тому же состоянию, что триггер APPLY_RESUME_SELECT, — обе
        # части одного и того же dropdown в реальном DOM.
        if selector == apply_form.APPLY_RESUME_DROPDOWN:
            # Панель списка резюме — то же состояние, что триггер: она открыта
            # ровно тогда, когда dropdown_opened (живой DOM: 0 элементов до
            # клика по триггеру, 1 после).
            return _FakeLocator(selector, self._state(apply_form.APPLY_RESUME_SELECT))
        if selector == apply_form.APPLY_RESUME_TOGGLE:
            return _FakeLocator(
                selector,
                self._state(apply_form.APPLY_RESUME_SELECT),
                resume_toggle=True,
            )
        if selector == apply_form.APPLY_RESUME_SELECT:
            return _FakeLocator(
                selector,
                self._state(selector),
                resume_title_inner=True,
            )
        m = re.match(rf"^\[data-qa='{apply_form.APPLY_RESUME_OPTION_PREFIX}(.*)'\]$", selector)
        if m:
            resume_id = m.group(1)
            return _FakeLocator(
                selector, self._state(apply_form.APPLY_RESUME_SELECT), option_resume_id=resume_id
            )
        return _FakeLocator(selector, self._state(selector))

    def wait_for_url(
        self, url_pattern: str, *, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        # #179: заменяет expect_navigation. Фиксируем timeout — регрессия #80:
        # двухшаговая навигация на форму отклика должна использовать потолок
        # GOTO_TIMEOUT_MS (медленный hh.ru), а не дефолт/короткий APPLY_TIMEOUT_MS.
        # wait_until фиксируем отдельно — регрессия B1 code-review: wait_for_url
        # БЕЗ явного wait_until дефолтится на "load" внутри Playwright (строже
        # domcontentloaded), поэтому вызывающий код обязан передавать "commit".
        self.navigation_entered += 1
        self.last_navigation_timeout = timeout
        self.last_navigation_wait_until = wait_until
        self.wait_for_url_calls.append((url_pattern, wait_until, timeout))
        if self.wait_for_url_error is not None:
            raise self.wait_for_url_error


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


def test_navigate_clicks_apply_button_and_waits_submit():
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    assert steps.navigate_to_response_form(page) is True

    # Для SPA-модалки URL не обязан меняться.
    assert page.navigation_entered == 0
    assert page._state(vacancy_page.VACANCY_APPLY_BUTTON).clicks == 1


def test_navigate_skips_expanded_hidden_resume_warning_without_url_wait():
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(vacancy_page.VACANCY_HIDDEN_RESUME_WARNING, True)

    reason = steps.navigate_to_response_form(page, "136173988")

    assert reason == "видимость резюме недостаточна для отклика"
    assert page.navigation_entered == 0


@pytest.mark.live_read
def test_hidden_resume_warning_uses_playwright_named_arg():
    """The visibility probe must pass its selector through Playwright's ``arg=`` API."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(
            """
            <div data-qa="hidden-resume-warning"
                 style="display:block;visibility:visible;max-height:100px"></div>
            """
        )

        assert steps._hidden_resume_warning_is_expanded(page) is True

        browser.close()


def test_navigate_does_not_raise_when_form_never_renders():
    # Форма (submit) не отрисовалась — ждём таймаут, логируем, но НЕ падаем.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    # submit намеренно отсутствует

    assert steps.navigate_to_response_form(page) is False

    assert page.navigation_entered == 0


def test_navigate_uses_bounded_random_dom_timeout_by_default(monkeypatch):
    # form_timeout_ms не передан -> должен выбираться jitter, а не фиксированный
    # APPLY_TIMEOUT_MS (обычный apply-цикл использует bounded random, не detect).
    randint_calls: list[tuple[int, int]] = []

    def _randint(minimum: int, maximum: int) -> int:
        randint_calls.append((minimum, maximum))
        return maximum

    monkeypatch.setattr(steps.random, "randint", _randint)
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    assert randint_calls == [
        (steps.RESPONSE_READY_MIN_TIMEOUT_MS, steps.RESPONSE_READY_MAX_TIMEOUT_MS)
    ]


def test_navigate_honors_explicit_form_timeout_ms_without_jitter(monkeypatch):
    # #442 cycle-review (F6): form_timeout_ms ранее принимался, но игнорировался
    # в теле функции — только navigation_timeout_ms реально влиял на ожидание
    # submit-кнопки. questionnaire.py/probe.py передают form_timeout_ms=5_000/
    # 10_000 рассчитывая управлять именно этим ожиданием — регрессия должна
    # провалить этот тест, если jitter снова перекроет явное значение.
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("random.randint не должен вызываться при явном form_timeout_ms")

    monkeypatch.setattr(steps.random, "randint", _fail_if_called)
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    assert steps.navigate_to_response_form(page, form_timeout_ms=5_000) is True
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).wait_for_timeout == 5_000


def test_navigate_form_timeout_ms_zero_is_honored_not_treated_as_falsy(monkeypatch):
    # 0 — валидное значение таймаута в этом файле (см. render_timeout_ms=0);
    # `form_timeout_ms or random.randint(...)` молча подменил бы 0 на jitter.
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("random.randint не должен вызываться при form_timeout_ms=0")

    monkeypatch.setattr(steps.random, "randint", _fail_if_called)
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page, form_timeout_ms=0)

    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).wait_for_timeout == 0


def test_navigate_uses_wait_for_url_not_expect_navigation():
    # #179: диагностика на боевом аккаунте показала, что клик по apply-кнопке
    # реально отправляет отклик (кнопка меняется на "уже откликнулись"), но
    # expect_navigation(wait_until='domcontentloaded') всё равно падал таймаутом
    # 90с — переход на /applicant/vacancy_response у залогиненного пользователя
    # рендерится как same-document/SPA-навигация (history.pushState), и
    # domcontentloaded не наступает, хотя URL меняется. wait_for_url следит
    # именно за URL — работает для обоих случаев.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    assert page.wait_for_url_calls == []


def test_navigate_wait_for_url_uses_commit_not_default_load():
    # #179 регрессия (B1, independent review): page.wait_for_url() реализован
    # через тот же expect_navigation внутри Playwright и БЕЗ явного wait_until
    # дефолтится на "load" — строже, чем domcontentloaded, который заменяли.
    # wait_until="commit" — единственное значение, не ждущее lifecycle-событие
    # документа вообще. Без явной передачи фикс не решал бы исходную проблему.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    assert page.last_navigation_wait_until is None


def test_navigate_wait_for_url_timeout_does_not_raise():
    # #179 регрессия: раньше TimeoutError из ожидания навигации не был перехвачен
    # steps.navigate_to_response_form — пробрасывался наверх необработанным и
    # ронял весь apply-цикл traceback'ом (реальный краш на боевом аккаунте,
    # vacancy_id=136221532). Клик мог реально уйти (симметрично #176
    # SubmitClickUncertain) — навигация не подтвердилась, но это не повод
    # крашить процесс: дальнейшие шаги (submit-кнопка/detect_questions) сами
    # определят, загрузилась ли форма.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    steps.navigate_to_response_form(page)  # не должен бросать

    apply_state = page._state(vacancy_page.VACANCY_APPLY_BUTTON)
    assert apply_state.clicks == 1


def test_navigate_wait_for_url_timeout_saves_diagnostics(tmp_path: Path, monkeypatch):
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    # submit intentionally absent: the form readiness timeout is diagnostic.
    monkeypatch.setattr(steps, "LOG_DIR", tmp_path)

    steps.navigate_to_response_form(page)

    assert page.screenshot_calls == 1
    assert page.content_calls == 1
    assert (tmp_path / "apply_form_timeout.png").exists()
    assert (tmp_path / "apply_form_timeout.html").exists()

    # Повторный таймаут БЕЗ vacancy_id перезаписывает те же файлы (идемпотентно
    # по stage, как probe.dump_probe_snapshot), а не копит файл на каждый retry.
    steps.navigate_to_response_form(page)

    assert page.screenshot_calls == 2
    assert len(list(tmp_path.glob("apply_form_timeout*.png"))) == 1
    assert len(list(tmp_path.glob("apply_form_timeout*.html"))) == 1


def test_navigate_wait_for_url_timeout_keeps_diagnostics_per_vacancy(tmp_path: Path, monkeypatch):
    # cycle-review round 2: round-1 fix сделал имя чисто по stage и потерял
    # контекст вакансии — при массовом apply-прогоне дамп одной вакансии
    # затирался следующей, что противоречит цели #192 (сравнить артефакты
    # разных вакансий). vacancy_id в имени разделяет их, оставаясь idempotent
    # per-vacancy.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    # submit intentionally absent: each vacancy gets its own form diagnostic.
    monkeypatch.setattr(steps, "LOG_DIR", tmp_path)

    steps.navigate_to_response_form(page, "111")
    steps.navigate_to_response_form(page, "222")

    assert (tmp_path / "apply_111_form_timeout.png").exists()
    assert (tmp_path / "apply_222_form_timeout.png").exists()


def test_navigate_wait_for_url_non_timeout_error_does_not_raise():
    # #179 (code-review): раньше wait_for_url ловил только PlaywrightTimeoutError,
    # хотя non-timeout PlaywrightError (page/context closed, navigation aborted)
    # тоже возможен и пробрасывался бы наверх необработанным, роняя весь цикл
    # apply по остальным вакансиям/резюме.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    page.wait_for_url_error = Error("Target page, context or browser has been closed")

    steps.navigate_to_response_form(page)  # не должен бросать

    apply_state = page._state(vacancy_page.VACANCY_APPLY_BUTTON)
    assert apply_state.clicks == 1


def test_navigate_apply_button_click_error_does_not_raise():
    # #179 (code-review): клик по apply-кнопке (до submit, до заполнения формы)
    # раньше не был обёрнут вообще — любой PlaywrightError из click() пробрасывался
    # наверх необработанным через pipeline._run (вызывается без try/except на
    # этой строке) и ронял весь цикл apply по остальным вакансиям/резюме. Ранний
    # отказ (#163, аналогично прочим шагам до submit) — не uncertain, просто fail
    # для текущей вакансии; wait_for_url/submit-поиск не должны выполняться после.
    page = FakeStepsPage()
    apply_state = page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    apply_state.click_error = Error("Target page, context or browser has been closed")
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)  # не должен бросать

    assert page.navigation_entered == 0  # wait_for_url не вызывался после сбойного клика


def test_navigate_missing_submit_saves_diagnostics(tmp_path: Path, monkeypatch):
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    monkeypatch.setattr(steps, "LOG_DIR", tmp_path)

    steps.navigate_to_response_form(page)

    assert page.screenshot_calls == 1
    assert page.content_calls == 1
    assert (tmp_path / "apply_form_timeout.png").exists()
    assert (tmp_path / "apply_form_timeout.html").exists()


def test_navigate_clicks_apply_button_with_no_wait_after():
    # #80 регрессия (cycle-2): Locator.click, триггерящий навигацию, имеет внутренний
    # шаг «wait for initiated navigations», ограниченный ACTION timeout
    # (set_default_timeout, дефолт 30с), а НЕ set_default_navigation_timeout. На
    # навигации 33с+ клик падал бы через 30с раньше 90с ожидания. Фикс:
    # no_wait_after=True — клик не ждёт навигацию сам; следующий явный
    # page.wait_for_url(timeout=GOTO_TIMEOUT_MS, #179) — единственный владелец
    # 90с ожидания.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    apply_state = page._state(vacancy_page.VACANCY_APPLY_BUTTON)
    assert apply_state.clicks == 1
    assert apply_state.click_kwargs == [{"no_wait_after": True}]


# --- fill_response_form: только обязательный submit ---


def test_fill_form_without_letter_toggle_still_fills_and_submits():
    """Минимальная форма: тоггла письма нет, но textarea развёрнута (случай
    136417846). Тоггл не кличем — он и не нужен; письмо заполняем, submit жмём.

    Раньше тест назывался test_fill_form_only_submit_present_... и утверждал,
    что письмо не трогается вовсе; это отражало прежний fail-open контракт,
    при котором отклик уходил пустым."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    submit = page._state(apply_form.APPLY_SUBMIT_BUTTON)
    assert submit.clicks == 1
    # Тоггла в DOM нет — кликать нечего, но письмо всё равно попало в форму.
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 0
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == ["письмо"]
    # The confirmed single option is inspected and selected before submit.
    assert page._state(apply_form.APPLY_RESUME_SELECT).clicks > 0


def test_fill_form_missing_submit_returns_reason_no_click():
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    # Submit отсутствует.
    # Письмо теперь обязательно (fail-closed): предмет этого теста — резюме/submit,
    # поэтому textarea делаем видимой, чтобы не срабатывал отказ по письму.
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "кнопка отправки отклика не найдена" in result


def test_fill_form_submit_click_error_raises_uncertain_marker():
    """#176: Playwright упал в момент submit-клика (navigation timeout после
    POST, target closed) — POST отклика МОГ уйти. Это принципиально не обычный
    отказ строкой (тот означает «отправки не было»): steps маркирует исход
    SubmitClickUncertain, а решение acted/uncertain/запись оставляет pipeline.
    Раньше исключение пробрасывалось сырым и валило цикл откликов до
    record_action/throttle.wait."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    submit = page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    submit.click_error = Error("Target page, context or browser has been closed")
    # Письмо теперь обязательно (fail-closed): предмет этого теста — submit-клик,
    # поэтому textarea делаем видимой, чтобы не срабатывал отказ по письму.
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    with pytest.raises(steps.SubmitClickUncertain):
        steps.fill_response_form(page, "RID", "письмо")


# --- fill_response_form: опциональные поля ---


def test_fill_form_with_letter_fills_textarea():
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TOGGLE, True)
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "тело письма")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 1
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == ["тело письма"]
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_no_letter_field_at_all_refuses_submit():
    """Смена контракта (было test_fill_form_letter_toggle_absent_skips_textarea).

    Раньше отсутствие и тоггла, и textarea означало «письмо опционально,
    отправляем без него» — молчаливый fail-open. Боевое измерение 2026-08-20
    (SSR topicList[].hasResponseLetter по всем 18 откликам аккаунта: с письмом 2,
    без письма 16) показало, что этим путём инструмент неделю слал пустые
    отклики: селектор тоггла (`vacancy-response-letter-toggle`) не совпадает
    в модалке ни разу.

    Письмо — смысл инструмента, а не опциональная деталь, поэтому теперь это
    fail-closed отказ ДО submit: следа на hh.ru нет, вакансия ретраится."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "письм" in result.lower()
    # Главное: submit НЕ нажат — пустой отклик не ушёл.
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_popup_letter_toggle_expands_and_fills_textarea():
    """Модалка hh.ru: тоггл письма — add-cover-letter (в actions-container, ВНЕ
    <form>), раскрывающий textarea vacancy-response-popup-form-letter-input.
    Подтверждено дампами apply_136190065/136190066 (2026-08-20)."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TOGGLE_POPUP, True)
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "тело письма")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == ["тело письма"]
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_pre_expanded_textarea_without_toggle_fills():
    """Случай успешного отклика 136417846: hh.ru отрендерил textarea уже
    развёрнутой (add-cover-letter отсутствует). Отсутствие тоггла легитимно —
    решает наличие textarea, а не тоггла."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "тело письма")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == ["тело письма"]
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_full_page_letter_shape_still_supported():
    """Регресс полной формы: vacancy-response-letter-toggle + form-letter-input
    (обе формы наблюдались в дампах 2026-08-16, full-page ветку не удаляем)."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TOGGLE, True)
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA_FORM, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "тело письма")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA_FORM).fills == ["тело письма"]
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_open_dropdown_blocks_submit_and_refuses():
    """Боевой случай 2026-08-20 (136190065/136190066): после клика по опции
    резюме React не закрыл dropdown; раскрытый listbox перекрыл submit-кнопку,
    Locator.click ретраил 30с (`subtree intercepts pointer events`) и падал
    в SubmitClickUncertain — ложная «неопределённость» при неотправленном
    отклике, которая жгла дневной лимит и навсегда блокировала вакансию.

    Теперь незакрывшийся список — честный отказ ДО submit."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    st.dropdown_auto_closes = False
    st.dropdown_stays_open = True
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    # Ключевое: submit не нажат и SubmitClickUncertain не возник.
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_closed_dropdown_proceeds_to_submit():
    """Штатный путь: панель закрылась после выбора → submit нажимается."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_closes_resume_panel_before_submit():
    """Если option-click не закрыл панель, закрываем её toggle-контейнером."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    st.dropdown_auto_closes = False
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    # 3 клика: открыть, выбрать опцию, закрыть fallback-toggle.
    assert st.clicks == 3
    assert st.resume_title_clicks == 1
    assert st.resume_toggle_clicks == 1
    assert st.dropdown_opened is False
    assert st.selected_resume_id == "RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_does_not_reopen_dropdown_after_option_auto_closes():
    """Live 2026-08-25: option-click уже закрыл drop-base; toggle открыл бы его снова."""
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.selected_resume_id == "RID"
    assert st.dropdown_opened is False
    assert st.resume_toggle_clicks == 0
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_resume_select_multiple_matches_selects_correct_resume():
    # Регрессия #6 (cycle-2 review): несколько резюме в dropdown —
    # клик по триггеру раскрывает опции; должна кликнуться опция с нужным resume_id,
    # а не первая попавшаяся. Оракул: среди [OTHER, RID] выбирается именно RID.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # Письмо теперь обязательно (fail-closed): предмет этого теста — резюме/submit,
    # поэтому textarea делаем видимой, чтобы не срабатывал отказ по письму.
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.selected_resume_id == "RID"
    # Панель закрыта повторным кликом по триггеру: пока она открыта, её оверлей
    # перекрывает submit (боевой случай 2026-08-20).
    assert st.dropdown_opened is False
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_resume_toggle_uses_role_button_ancestor_for_close():
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)

    page.locator(apply_form.APPLY_RESUME_SELECT).first.click()
    page.locator(apply_form.APPLY_RESUME_SELECT).first.click()
    assert st.dropdown_opened is True

    page.locator(apply_form.APPLY_RESUME_TOGGLE).click()
    assert st.dropdown_opened is False


# --- fill_response_form: выбор резюме fail-closed (#33) ---
#
# Регрессия #33: ранее при отсутствии опции с совпадающим resume_id форма всё равно
# отправлялась (fail-open — submit кликался, уходило резюме по умолчанию). Теперь
# неоднозначность/отсутствие нужного резюме = отказ: submit НЕ нажимается, возвращается
# причина. живой DOM: сопоставление больше не парсит href (его на форме нет) — опция
# резюме адресуется напрямую по resume_id в data-qa, поэтому лжесовпадения по
# префиксу/суффиксу структурно невозможны (были актуальны только для href-парсинга).


def test_fill_form_resume_no_match_does_not_submit_returns_reason():
    # Опции есть, но ни одна не содержит запрошенный resume_id.
    # Оракул: submit НЕ нажат, возвращена причина отказа (а не None).
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER1", "OTHER2"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_single_match_submits_and_returns_none():
    # Ровно одна опция с совпадающим resume_id → выбор успешен, submit нажат, None.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID", "THIRD"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # Письмо теперь обязательно (fail-closed): предмет этого теста — резюме/submit,
    # поэтому textarea делаем видимой, чтобы не срабатывал отказ по письму.
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.selected_resume_id == "RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


# --- выбор резюме: долгое ожидание селектора (cycle-2 review, Codex) ---
#
# Codex cycle-1: короткий OPTIONAL_FIELD_TIMEOUT_MS (1.5с) для селектора резюме
# пропускает выбор при медленном JS-рендере залогиненной формы на multi-resume
# аккаунте → submit отправляет резюме по умолчанию (fail-open). Селектор резюме —
# критичнее cover-letter: ждём его как обязательный элемент (APPLY_TIMEOUT_MS), и
# только отсутствие после долгого ожидания = «на этой странице выбора нет».


def test_resume_select_uses_full_timeout_not_optional():
    # Оракул: ожидание селектора резюме — APPLY_TIMEOUT_MS, не OPTIONAL_FIELD_TIMEOUT_MS.
    # Это закрывает гонку рендера (селектор может появиться позже submit-кнопки).
    assert steps.RESUME_SELECT_TIMEOUT_MS >= steps.APPLY_TIMEOUT_MS


def test_fill_form_resume_select_absent_does_not_submit():
    # Красный тест #340: отсутствие подтверждённого триггера нельзя трактовать
    # как «у аккаунта одно резюме» — иначе hh.ru прикладывает default-резюме.
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # APPLY_RESUME_SELECT намеренно отсутствует.

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме не подтверждено" in result
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_option_not_visible_after_open_does_not_submit():
    # живой DOM: single-resume аккаунт (или resume_id не входит в форму этой вакансии) —
    # триггер есть, но после клика опция с нужным resume_id не появляется. Оракул:
    # fail-closed отказ (#33 инвариант) — не тихий submit дефолтного резюме.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = []  # dropdown раскрылся, но нужной опции нет
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_reorder_after_open_clicks_correct_resume():
    # TOCTOU (Codex cycle-3 принцип, живой DOM): JS переупорядочил опции между
    # открытием dropdown и кликом. Оракул: identity-bound выбор адресует опцию по
    # resume_id напрямую (не по индексу), поэтому reorder не влияет на результат.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID"]  # порядок на открытии
    st.reorder_to = ["RID", "OTHER"]  # порядок к моменту клика — резолвится тем же ID
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # Письмо теперь обязательно (fail-closed): предмет этого теста — резюме/submit,
    # поэтому textarea делаем видимой, чтобы не срабатывал отказ по письму.
    page.set_visible(apply_form.APPLY_COVER_LETTER_TEXTAREA, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.selected_resume_id == "RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_resume_option_disappears_after_wait_does_not_submit():
    # TOCTOU: опция была видима в wait_for(state="visible"), но исчезла к
    # финальной проверке count()==1 (transient re-render/drift между двумя
    # вызовами Playwright — тот же принцип, что #340 применял к триггеру).
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    st.disappear_after_wait = True  # wait_for(visible) проходит, count() → 0
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_dup_appears_at_click_time_does_not_submit():
    # TOCTOU (Codex cycle-3 принцип, живой DOM): к моменту клика JS вставил дубль
    # опции с тем же resume_id (аномалия разметки). Strict-локатор → >1 совпадение →
    # Error → отказ, submit не нажат.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID"]  # одна RID на открытии
    st.reorder_to = ["RID", "RID"]  # две RID к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_target_disappears_at_click_time_does_not_submit():
    # TOCTOU (Codex cycle-3 verify принцип, живой DOM): нужная опция исчезла
    # между открытием и кликом. Оракул: fail-closed отказ, submit не нажат.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID"]  # RID есть при открытии
    st.reorder_to = ["OTHER", "THIRD"]  # RID исчез к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_wait_runtime_error_does_not_submit():
    # Codex cycle-5 принцип: не-timeout PlaywrightError на ожидании ТРИГГЕРА не
    # должен маскироваться под «выбора нет» — ловится только PlaywrightTimeoutError,
    # прочие PlaywrightError → отказ. Оракул: submit НЕ нажат.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["OTHER", "RID"]
    st.wait_error = True  # wait_for(attached) на триггере кидает generic PlaywrightError
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_trigger_click_error_does_not_submit():
    # живой DOM: клик по триггеру (раскрытие dropdown) сам может упасть — тот же
    # ранний-отказ принцип, что и у apply-кнопки (#163): submit не нажат.
    page = FakeStepsPage()
    st = page.set_visible(apply_form.APPLY_RESUME_SELECT, True)
    st.option_resume_ids = ["RID"]
    st.trigger_click_error = Error("detached")
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


# --- константы ---


def test_optional_field_timeout_is_short():
    # Опциональные поля ждут недолго: отсутствие — это норма, не долгоиграющая ошибка.
    assert steps.OPTIONAL_FIELD_TIMEOUT_MS < steps.APPLY_TIMEOUT_MS
