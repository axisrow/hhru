"""Characterization-тесты apply/steps: явные ожидания Playwright (#6).

Без браузера — через FakePage, имитирующий минимальный Playwright API, который
использует steps.py: locator(...).wait_for(state='visible', timeout=...),
click(), fill(), wait_for_url(). Страхуют поведение wait'ов: time.sleep
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
from hhru_bot.browser import GOTO_TIMEOUT_MS
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
        return _FakeLocator(self.selector, self._state, strict=False)

    def __init__(
        self,
        selector: str,
        state: _SelectorState,
        *,
        strict: bool = True,
        href_filter: str | None = None,
    ) -> None:
        self.selector = selector
        self._state = state
        self._strict = strict
        # href_filter: селектор уточнён [href='...'] — strict-локатор, привязанный к
        # identity. click()/count() резолвятся по живым href с учётом reorder (cycle-3).
        self._href_filter = href_filter

    def _live_hrefs(self) -> list[str]:
        # Текущие «живые» href опций. href-локатор моделирует «момент клика» — после
        # scan, поэтому при заданном reorder он всегда отражает post-scan DOM. Обычный
        # nth-доступ активирует reorder после полного scan (nth_calls > match_count).
        hrefs = self._state.option_hrefs
        if self._href_filter is not None:
            if self._state.reorder_to:
                hrefs = self._state.reorder_to
        elif self._state.reorder_to and self._state._nth_calls > self._state.match_count:
            hrefs = self._state.reorder_to
        return hrefs

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:  # noqa: ARG002
        # Моделируем реальное поведение Playwright: в strict mode для коллекции
        # (несколько резюме) wait_for кидает обычный Error (НЕ PlaywrightTimeoutError).
        # Через .first strict mode снимается — тогда ждём готовность коллекции.
        if self._state.wait_error:
            # Cycle-5: имитация не-timeout PlaywrightError (runtime/selector failure).
            raise Error(f"runtime error waiting for {self.selector}")
        if self._state.is_collection and self._strict and self._href_filter is None:
            raise Error(  # noqa: TRY002 — имитация playwright._impl._errors.Error
                f"strict mode violation: {self.selector} resolved to "
                f"{self._state.match_count} elements"
            )
        if state == "attached":
            # state='attached' — наличие в DOM (match_count>0 для коллекции), без
            # требования видимости. Моделирует cycle-4 фикс: скрытый первый элемент
            # всё равно «прикреплён», и count() решает, есть ли выбор.
            if self._state.is_collection:
                if self._state.match_count == 0:
                    raise PlaywrightTimeoutError(f"{self.selector} not attached")
                return
            if not self._state.visible:
                raise PlaywrightTimeoutError(f"{self.selector} not attached")
            return
        if not self._state.visible:
            raise PlaywrightTimeoutError(f"{self.selector} not visible")

    def click(self, **kwargs) -> None:
        # Фиксируем kwargs клика — регрессия #80: клик apply-кнопки, триггерящий
        # навигацию, должен идти с no_wait_after=True (ожидание навигации владеет
        # внешний 90с expect_navigation, а не внутренний 30с action-timeout клика).
        self._state.click_kwargs.append(kwargs)
        # #176: имитация Playwright-исключения в момент клика (navigation timeout
        # после POST, target closed) — действие могло уйти на hh.ru.
        if self._state.click_error is not None:
            raise self._state.click_error
        if self._href_filter is not None:
            # Strict href-локатор: ровно одна живая опция с этим href, иначе Error
            # (как реальный Playwright strict mode при != 1 совпадении).
            matches = [h for h in self._live_hrefs() if h == self._href_filter]
            if len(matches) != 1:
                raise Error(  # noqa: TRY002
                    f"strict mode violation: {self.selector} resolved to {len(matches)} "
                    f"elements (href={self._href_filter!r})"
                )
            self._state.current_href = matches[0]
        self._state.clicks += 1

    def fill(self, value: str) -> None:
        self._state.fills.append(value)

    def count(self) -> int:
        # Для коллекции (резюме, set_match_count) count() = число совпадений в DOM.
        # Иначе — одиночный элемент: 1 если visible, иначе 0.
        if self._state.is_collection:
            if self._state.disappear_after_wait:
                return 0  # TOCTOU: селектор исчез между wait_for и count
            return self._state.match_count
        return 1 if self._state.visible else 0

    def get_attribute(self, _name: str) -> str | None:
        return self._state.current_href

    def nth(self, i: int) -> _FakeLocator:
        # Каждая опция резюме — свой href; _select_resume_in_form ищет resume_id в нём.
        self._state._nth_calls += 1
        if self._state.option_hrefs:
            hrefs = self._state.option_hrefs
            # reorder_to: после того как scan прочитал все опции один раз
            # (_nth_calls > match_count), DOM «меняется» и последующие nth() берут
            # href из reorder_to. Старый код кликал по сохранённому индексу скана →
            # попадал на WRONG опцию; identity-bound фикс пере-сканирует и кликает верно.
            if self._state.reorder_to and self._state._nth_calls > self._state.match_count:
                hrefs = self._state.reorder_to
            self._state.current_href = hrefs[i]
        self._state.clicks += 1  # клик по выбранной опции
        return self

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() объединяет apply-button и
        # already-responded-маркеры одним локатором. Фейк комбинирует state:
        # visible/match_count по OR, чтобы wait_for/count отражали объединение.
        combined = _SelectorState(visible=self._state.visible or other._state.visible)
        combined.is_collection = self._state.is_collection or other._state.is_collection
        combined.match_count = max(self._state.match_count, other._state.match_count)
        combined.wait_error = self._state.wait_error or other._state.wait_error
        return _FakeLocator(f"({self.selector})|({other.selector})", combined, strict=False)


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
        # Для коллекции резюме: href каждой опции (current_href ставится в nth()).
        self.option_hrefs: list[str] = []
        self.current_href: str | None = None
        # Имитация TOCTOU: селектор был видим в wait_for, но исчез к count().
        # Моделирует transient re-render/drift между двумя вызовами Playwright.
        self.disappear_after_wait = False
        # Имитация reorder-TOCTOU (cycle-3): после того как scan прочитал все опции
        # один раз, последующие nth() берут href из reorder_to. Моделирует
        # переупорядочивание/вставку опций JS между scan и click.
        self.reorder_to: list[str] | None = None
        self._nth_calls = 0
        # Имитация не-timeout PlaywrightError в wait_for (cycle-5): runtime/selector
        # failure, который НЕ должен маскироваться под «выбора нет».
        self.wait_error = False
        # #176: PlaywrightError в момент click() — клик мог уйти (POST отправлен,
        # но ожидание после клика упало). None = обычный успешный клик.
        self.click_error: Exception | None = None


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

    def screenshot(self, **_kwargs) -> bytes:
        self.screenshot_calls += 1
        return b"png"

    def content(self) -> str:
        self.content_calls += 1
        return "<html>diagnostic</html>"

    def wait_for_function(self, _expression, arg=None, timeout=None):
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
        # Селектор опции резюме по точному href: BASE[href='...']. Резолвится к тому же
        # состоянию коллекции APPLY_RESUME_SELECT, но с href_filter для strict-клика
        # в момент действия (cycle-3 identity-bound выбор).
        m = re.match(r"^(.+?)\[href='(.*)'\]$", selector)
        if m:
            base, href = m.group(1), m.group(2).replace("\\'", "'")
            return _FakeLocator(selector, self._state(base), href_filter=href)
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

    # Клик по apply-кнопке + ожидание URL через wait_for_url (#179).
    assert page.navigation_entered == 1
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

    assert page.navigation_entered == 1


def test_navigate_uses_goto_timeout_for_form_navigation():
    # #80 регрессия: двухшаговая навигация на форму отклика (wait_for_url после
    # клика по apply-кнопке, #179) — это сетевая навигация hh.ru, которая под
    # DDoS-Guard грузится 33с+. Де­фолт/короткий APPLY_TIMEOUT_MS (10с) тут
    # падает; потолок должен быть GOTO_TIMEOUT_MS, как и у всех goto в проекте.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    steps.navigate_to_response_form(page)

    assert page.last_navigation_timeout == GOTO_TIMEOUT_MS


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

    assert page.wait_for_url_calls == [
        ("**/applicant/vacancy_response**", "commit", GOTO_TIMEOUT_MS)
    ]


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

    assert page.last_navigation_wait_until == "commit"


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
    page.wait_for_url_error = PlaywrightTimeoutError("Timeout 90000ms exceeded.")

    steps.navigate_to_response_form(page)  # не должен бросать

    apply_state = page._state(vacancy_page.VACANCY_APPLY_BUTTON)
    assert apply_state.clicks == 1


def test_navigate_wait_for_url_timeout_saves_diagnostics(tmp_path: Path, monkeypatch):
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    page.wait_for_url_error = PlaywrightTimeoutError("navigation timeout")
    monkeypatch.setattr(steps, "LOG_DIR", tmp_path)

    steps.navigate_to_response_form(page)

    assert page.screenshot_calls == 1
    assert page.content_calls == 1
    assert (tmp_path / "apply_navigation_timeout.png").exists()
    assert (tmp_path / "apply_navigation_timeout.html").exists()

    # Повторный таймаут БЕЗ vacancy_id перезаписывает те же файлы (идемпотентно
    # по stage, как probe.dump_probe_snapshot), а не копит файл на каждый retry.
    steps.navigate_to_response_form(page)

    assert page.screenshot_calls == 2
    assert len(list(tmp_path.glob("apply_navigation_timeout*.png"))) == 1
    assert len(list(tmp_path.glob("apply_navigation_timeout*.html"))) == 1


def test_navigate_wait_for_url_timeout_keeps_diagnostics_per_vacancy(tmp_path: Path, monkeypatch):
    # cycle-review round 2: round-1 fix сделал имя чисто по stage и потерял
    # контекст вакансии — при массовом apply-прогоне дамп одной вакансии
    # затирался следующей, что противоречит цели #192 (сравнить артефакты
    # разных вакансий). vacancy_id в имени разделяет их, оставаясь idempotent
    # per-vacancy.
    page = FakeStepsPage()
    page.set_visible(vacancy_page.VACANCY_APPLY_BUTTON, True)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    page.wait_for_url_error = PlaywrightTimeoutError("navigation timeout")
    monkeypatch.setattr(steps, "LOG_DIR", tmp_path)

    steps.navigate_to_response_form(page, "111")
    steps.navigate_to_response_form(page, "222")

    assert (tmp_path / "apply_111_navigation_timeout.png").exists()
    assert (tmp_path / "apply_222_navigation_timeout.png").exists()


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


def test_fill_form_only_submit_present_clicks_submit_returns_none():
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    submit = page._state(apply_form.APPLY_SUBMIT_BUTTON)
    assert submit.clicks == 1
    # Опциональные поля не трогались.
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 0
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == []
    # The confirmed single option is inspected and selected before submit.
    assert page._state(apply_form.APPLY_RESUME_SELECT).clicks > 0


def test_fill_form_missing_submit_returns_reason_no_click():
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID"]
    # Submit отсутствует.

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
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID"]
    submit = page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    submit.click_error = Error("Target page, context or browser has been closed")

    with pytest.raises(steps.SubmitClickUncertain):
        steps.fill_response_form(page, "RID", "письмо")


# --- fill_response_form: опциональные поля ---


def test_fill_form_with_letter_fills_textarea():
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID"]
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
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert page._state(apply_form.APPLY_COVER_LETTER_TOGGLE).clicks == 0
    assert page._state(apply_form.APPLY_COVER_LETTER_TEXTAREA).fills == []


def test_fill_form_resume_select_multiple_matches_selects_correct_resume():
    # Регрессия #6 (cycle-2 review): APPLY_RESUME_SELECT — коллекция (несколько резюме).
    # wait_for в strict mode при >1 совпадении кидает обычный Error. Проверка «есть ли
    # поле резюме» НЕ должна идти через _is_visible/wait_for — иначе выбор резюме
    # пропускается и submit отправляет резюме по умолчанию (не запрошенный resume_id).
    # Оракул: при двух резюме с разными href должна кликнуться опция, содержащая resume_id.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    # Выбор резюме вызвался и кликнул именно опцию с RID (current_href = 2-я опция).
    assert result is None
    assert st.current_href == "/resume/RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


# --- fill_response_form: выбор резюме fail-closed (#33) ---
#
# Регрессия #33: ранее при отсутствии опции с совпадающим resume_id форма всё равно
# отправлялась (fail-open — submit кликался, уходило резюме по умолчанию). Теперь
# неоднозначность/отсутствие нужного резюме = отказ: submit НЕ нажимается, возвращается
# причина. Совпадение resume_id проверяется как сегмент пути (/resume/{id} или
# resume_id={id}), не как голая подстрока href — снижает случайные лжесовпадения.


def test_fill_form_resume_no_match_does_not_submit_returns_reason():
    # Коллекция резюме есть, но ни одна опция не содержит запрошенный resume_id.
    # Оракул: submit НЕ нажат, возвращена причина отказа (а не None).
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER1", "/resume/OTHER2"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_ambiguous_match_does_not_submit_returns_reason():
    # Две опции совпадают по resume_id — неоднозначность = отказ, а не «кликни первое».
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/RID", "/resume/RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_single_match_submits_and_returns_none():
    # Ровно одна опция с совпадающим resume_id → выбор успешен, submit нажат, None.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 3)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID", "/resume/THIRD"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.current_href == "/resume/RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_resume_match_requires_path_segment_not_bare_substring():
    # resume_id должен совпасть как сегмент пути, а не как подстрока где попало.
    # Здесь "RID" — лишь подстрока чужого href /resume/PONDERING, сегмента /resume/RID нет.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/PONDERING"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


# --- fill_response_form: точное совпадение сегмента (cycle-2 review, Codex) ---
#
# Codex cycle-1: совпадение как сегмент всё ещё подстрока — /resume/RID2 и
# other_resume_id=RID ложно матчат resume_id=RID → клик неверной опции → submit.
# Оракул: точное равенство сегмента пути/значения query, без префиксных/суффиксных
# лжесовпадений.


def test_fill_form_resume_match_rejects_suffix_in_path_segment():
    # /resume/RID2 НЕ должно совпадать с resume_id="RID": это чужое резюме.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/resume/RID2"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_match_rejects_similarly_named_query_param():
    # ?other_resume_id=RID НЕ должно совпадать: совпадает только resume_id=RID.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/app/applicant/vacancy_response?other_resume_id=RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_match_accepts_resume_id_query_param():
    # Позитивный контроль: ?resume_id=RID (точное значение) → выбор успешен.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 1)
    st.option_hrefs = ["/app/applicant/vacancy_response?resume_id=RID&topic=1"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
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
    # Красный тест #340: отсутствие подтверждённого селектора нельзя трактовать
    # как «у аккаунта одно резюме» — иначе hh.ru прикладывает default-резюме.
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # APPLY_RESUME_SELECT намеренно отсутствует.

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме не подтверждено" in result
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_selector_disappears_after_detect_does_not_submit():
    # TOCTOU (Codex cycle-2): селектор выбора резюме был видим в wait_for, но исчез
    # к count() (transient re-render/drift). Оракул: submit НЕ нажат, возвращена
    # причина отказа — не отправляем резюме по умолчанию при нестабильном селекторе.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/RID", "/resume/OTHER"]
    st.disappear_after_wait = True  # wait_for проходит, count() → 0
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert "резюме" in result.lower()
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_reorder_after_scan_clicks_correct_resume():
    # TOCTOU (Codex cycle-3): JS переупорядочил опции между scan и click. Scan видит
    # [OTHER, RID] (RID на индексе 1); к моменту клика DOM = [RID, OTHER] (RID на 0).
    # Оракул: identity-bound выбор пере-сканирует href в момент клика и кликает RID
    # (а не сохранённый индекс 1, который теперь = OTHER).
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]  # порядок на scan
    st.reorder_to = ["/resume/RID", "/resume/OTHER"]  # порядок к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert st.current_href == "/resume/RID"  # кликнули RID, не OTHER
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_resume_dup_appears_at_click_time_does_not_submit():
    # TOCTOU (Codex cycle-3): к моменту клика href резюме задвоился (JS вставил дубль).
    # href-локатор strict → >1 совпадение → Error → отказ, submit не нажат.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]  # одна RID на scan
    st.reorder_to = ["/resume/RID", "/resume/RID"]  # две RID к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_target_disappears_at_click_time_does_not_submit():
    # TOCTOU (Codex cycle-3 verify): target-href исчез между scan и click.
    # href-локатор strict → 0 совпадений → Error → отказ, submit не нажат.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]  # RID есть на scan
    st.reorder_to = ["/resume/OTHER", "/resume/THIRD"]  # RID исчез к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


def test_fill_form_resume_hidden_first_match_still_selects_not_submit_default():
    # Codex cycle-4: .first.wait_for(state='visible') видел только первый DOM-матч.
    # Если первый скрыт, а другой видим — старый код таймаутил → «выбора нет» → submit
    # дефолтного резюме. Фикс: wait_for(state='attached') + решающая проверка по count().
    # Оракул: коллекция прикреплена (count=2) → выбор вызывается → клик по RID.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.visible = False  # первый элемент скрыт → wait_for(state='visible') таймаутил бы
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    # С фиксом: count()==2 → выбор → клик RID, submit нажат.
    assert result is None
    assert st.current_href == "/resume/RID"
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


def test_fill_form_resume_wait_runtime_error_does_not_submit():
    # Codex cycle-5: except ловил ЛЮБОЙ PlaywrightError → options_count=0. Не-timeout
    # runtime/selector failure маскировался под «выбора нет» → skip count()/выбор →
    # submit дефолтного резюме. Фикс: ловим только PlaywrightTimeoutError; прочие
    # PlaywrightError → отказ. Оракул: submit НЕ нажат.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]
    st.wait_error = True  # wait_for кидает generic PlaywrightError (НЕ timeout)
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


# --- константы ---


def test_optional_field_timeout_is_short():
    # Опциональные поля ждут недолго: отсутствие — это норма, не долгоиграющая ошибка.
    assert steps.OPTIONAL_FIELD_TIMEOUT_MS < steps.APPLY_TIMEOUT_MS
