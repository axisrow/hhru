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

    @property
    def first(self):
        # В реальном Playwright .first снимает strict mode: wait_for на коллекции
        # через .first проходит (ждёт первый совпавший), а на всей коллекции без .first
        # кидает strict-mode Error. Возвращаем локатор с _strict=False.
        return _FakeLocator(self.selector, self._state, strict=False)

    def __init__(self, selector: str, state: _SelectorState, *, strict: bool = True) -> None:
        self.selector = selector
        self._state = state
        self._strict = strict

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:  # noqa: ARG002
        # Моделируем реальное поведение Playwright: в strict mode для коллекции
        # (несколько резюме) wait_for кидает обычный Error (НЕ PlaywrightTimeoutError).
        # Через .first strict mode снимается — тогда ждём видимость коллекции.
        if self._state.is_collection and self._strict:
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


class _SelectorState:
    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        # is_collection=True имитирует коллекцию (несколько резюме): wait_for кидает
        # strict-mode Error, count() возвращает match_count.
        self.is_collection = False
        self.match_count = 1
        self.clicks = 0
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
        """Селектор совпал с `count` элементами → коллекция (имитация strict-mode)."""
        st = self._state(selector)
        st.is_collection = True
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


def test_fill_form_resume_select_absent_single_resume_submits():
    # Happy path одного резюме: выбора нет (селектор отсутствует после долгого ожидания)
    # → submit жмётся. Не ломаем аккаунты с одним резюме.
    page = FakeStepsPage()
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)
    # APPLY_RESUME_SELECT намеренно отсутствует.

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 1


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
    # Live-скан находит >1 опции с target_href → неоднозначно → отказ, submit не нажат.
    page = FakeStepsPage()
    st = page.set_match_count(apply_form.APPLY_RESUME_SELECT, 2)
    st.option_hrefs = ["/resume/OTHER", "/resume/RID"]  # одна RID на scan
    st.reorder_to = ["/resume/RID", "/resume/RID"]  # две RID к моменту клика
    page.set_visible(apply_form.APPLY_SUBMIT_BUTTON, True)

    result = steps.fill_response_form(page, "RID", "письмо")

    assert result is not None
    assert page._state(apply_form.APPLY_SUBMIT_BUTTON).clicks == 0


# --- константы ---


def test_optional_field_timeout_is_short():
    # Опциональные поля ждут недолго: отсутствие — это норма, не долгоиграющая ошибка.
    assert steps.OPTIONAL_FIELD_TIMEOUT_MS < steps.APPLY_TIMEOUT_MS
