"""Characterization-тесты apply/pipeline: оркестрация шагов.

Без браузера — через FakePage, имитирующий минимальный Playwright API,
используемый в шагах. Страхуют, что декомпозиция не изменила поведение
отклика (dry-run путь, уже откликались, кнопка не найдена, успех).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hhru_bot.apply.pipeline as pipeline_module
from hhru_bot.apply import ProbeHook, apply_to_vacancy
from hhru_bot.history import SKIP_REASONS
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.integration


class _FakeLocator:
    @property
    def first(self):
        return self

    def __init__(
        self,
        present: bool = False,
        attrs: dict[str, str] | None = None,
        click_error: Exception | None = None,
        wait_for_calls: list[int] | None = None,
        wait_for_timeouts: list[float] | None = None,
    ):
        self._present = present
        self._attrs = attrs or {}
        # #176: PlaywrightError в момент click() (клик мог уйти на hh.ru).
        self._click_error = click_error
        # #226 cycle-review: общий счётчик wait_for-вызовов, разделяемый через
        # .or_() — проверяет, что wait_apply_button делает РОВНО один wait_for
        # на объединённом локаторе, а не последовательно кнопка-потом-маркеры
        # (что копило бы полный APPLY_TIMEOUT_MS на each already-responded вакансии).
        self._wait_for_calls = wait_for_calls if wait_for_calls is not None else []
        # #241 cycle-review round 2: фиксирует переданный timeout каждого
        # wait_for-вызова — проверяет, что check_already_responded всегда
        # получает полный _VISIBILITY_CHECK_TIMEOUT_MS (round 1 пробовал
        # передавать 0 при подтверждённой кнопке, отклонено дважды).
        self._wait_for_timeouts = wait_for_timeouts if wait_for_timeouts is not None else []

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        self._wait_for_calls.append(1)
        self._wait_for_timeouts.append(timeout)
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, **_kwargs) -> None:
        if self._click_error is not None:
            raise self._click_error
        return None

    def fill(self, _value: str) -> None:
        return None

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def nth(self, _i: int) -> _FakeLocator:
        return self

    def locator(self, _selector: str) -> _FakeLocator:
        # Chained locator (используется #95 heuristic-скоупингом внутри найденной
        # <form>) — фейк не различает вложенность, считает "ничего внутри" (0),
        # т.к. тесты этого файла не проверяют heuristic-содержимое формы, только
        # сам факт resolve/no-resolve form-scope (indeterminate-путь).
        return _FakeLocator(present=False)

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() ждёт кнопку ИЛИ already-responded
        # маркеры одним локатором — фейк комбинирует "present", если хотя бы один
        # из объединяемых локаторов присутствует; wait_for-вызовы объединённого
        # локатора продолжают писаться в тот же общий счётчик.
        return _FakeLocator(
            present=self._present or other._present,
            wait_for_calls=self._wait_for_calls,
            wait_for_timeouts=self._wait_for_timeouts,
        )

    def filter(self, *, visible: bool | None = None) -> _FakeLocator:  # noqa: ARG002
        # #248 cycle-review round 2: filter(visible=True) narrows the union to
        # visible matches before .first resolves. The fake only models a single
        # present/absent boolean per selector (no hidden-vs-visible distinction
        # within one _present state), so filtering is a no-op here — presence
        # already implies visibility in this fake's model.
        return self


class FakePage:
    """Имитирует Playwright Page для путей pipeline. Настраивает «состояние» страницы."""

    def __init__(
        self,
        *,
        apply_button: bool = True,
        already_responded: bool = False,
        success: bool = True,
        submit_in_form: bool = False,
        submit_click_error: Exception | None = None,
    ):
        self.url = ""
        self.goto_calls: list[str] = []
        self._apply_button = apply_button
        self._already_responded = already_responded
        self._success = success
        # #95 round-2: submit обёрнут в <form> (детектится xpath=ancestor::form[1]
        # в apply/questions.py::_form_scope) — по умолчанию False, чтобы явно
        # моделировать indeterminate-путь там, где тест его не настраивает.
        self._submit_in_form = submit_in_form
        # #176: PlaywrightError в момент submit-клика (клик мог уйти).
        self._submit_click_error = submit_click_error
        # #226 cycle-review: общий счётчик wait_for-вызовов apply-button/
        # already-responded локаторов — считает, что wait_apply_button ждёт
        # объединённым локатором (1 wait_for), а не последовательно.
        self.apply_wait_for_calls: list[int] = []
        # #241 cycle-review round 2: фиксирует переданные already-responded
        # wait_for-таймауты отдельно от общего apply_wait_for_calls счётчика —
        # проверяет, что они всегда равны полному _VISIBILITY_CHECK_TIMEOUT_MS.
        self.already_responded_wait_for_timeouts: list[float] = []

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.apply import success
        from hhru_bot.selector_groups import apply_form, vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(
                present=self._apply_button, wait_for_calls=self.apply_wait_for_calls
            )
        if selector in (
            vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN,
            vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT,
        ):
            return _FakeLocator(
                present=self._already_responded,
                wait_for_calls=self.apply_wait_for_calls,
                wait_for_timeouts=self.already_responded_wait_for_timeouts,
            )
        if selector == success.APPLY_SUCCESS_MARKER:
            return _FakeLocator(present=self._success)
        if selector == apply_form.APPLY_RESUME_SELECT:
            # A confirmed resume control is required before a non-dry-run submit.
            # Keep the pipeline fake aligned with the fail-closed production path.
            return _FakeLocator(present=self._success, attrs={"href": "/resume/RID"})
        if selector == f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]":
            return _FakeLocator(present=self._success and self._submit_in_form)
        # Прочие селекторы формы — считаем отсутствующими (форма не заполнена,
        # но submit присутствует в фейковом успехе через success-путь ниже).
        if selector == apply_form.APPLY_SUBMIT_BUTTON:
            return _FakeLocator(present=self._success, click_error=self._submit_click_error)
        return _FakeLocator(present=False)

    def wait_for_url(self, _url_pattern, **_kwargs):
        # #179: navigate_to_response_form больше не использует expect_navigation.
        return None


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id="1", title="Dev", company="Acme", url="https://hh.ru/vacancy/1")


# --- dry-run ---


def test_apply_dry_run_success():
    page = FakePage(apply_button=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "Здравствуйте, {company_name}", dry_run=True)
    assert result.success is True
    assert result.reason == "dry-run"
    assert page.goto_calls == ["https://hh.ru/vacancy/1"]
    assert result.acted is False  # #163: симуляция без submit — без паузы


def test_apply_login_form_is_checked_after_navigation(monkeypatch):
    page = FakePage()
    events: list[str] = []

    def fake_goto(p, url, **_kwargs):
        events.append("goto")
        p.goto(url)

    def fake_has_login_form(_page):
        events.append("auth")
        assert events == ["goto", "auth"]
        return True

    monkeypatch.setattr(pipeline_module, "goto_hh", fake_goto)
    monkeypatch.setattr(pipeline_module, "has_login_form", fake_has_login_form)

    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)

    assert result.success is False
    assert "Сессия недействительна" in result.reason
    assert events == ["goto", "auth"]
    assert result.acted is False  # #163: провал до submit — без паузы и записи


def test_apply_already_responded_not_deduped_by_dom():
    # #3: мёртвый DOM-маркер «уже откликались» убран. Дедупликация идёт через
    # history.has_applied() в filter_candidates() ещё до apply_to_vacancy, поэтому
    # check_already_responded на странице вакансии ничего не отсекает — вакансия
    # доходит до кнопки отклика и идёт по обычному пути (здесь — dry-run стоп на
    # письме). Раньше этот тест симулировал already-responded состояние страницы,
    # но после удаления маркера моделировать его больше нечем и не нужно.
    page = FakePage(apply_button=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)
    assert result.success is True
    assert result.reason == "dry-run"


def test_apply_no_apply_button():
    page = FakePage(apply_button=False)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)
    assert result.success is False
    assert "кнопка отклика не найдена" in result.reason
    assert result.acted is False  # #163: до submit — без паузы и записи


def test_apply_already_responded_is_skip_not_missing_button_failure():
    page = FakePage(apply_button=False, already_responded=True)

    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)

    assert result.success is False
    assert result.skipped is True
    assert result.reason == "уже откликались по вакансии 1, пропуск"
    assert result.acted is False


def test_apply_transitional_both_markers_prefers_already_responded():
    """A transient page showing both markers must fail closed to skip."""
    page = FakePage(apply_button=True, already_responded=True)

    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)

    assert result.skipped is True
    assert result.skip_reason == "already_applied"


def test_apply_already_responded_check_always_blocks_regardless_of_button():
    """#241 cycle-review round 2: попытка пропускать блокирующее ожидание, когда
    кнопка уже найдена (round 1), была отклонена ДВАЖДЫ — codex указал на гонку
    состояний (маркер может отрендериться сразу после кнопки), /review указал на
    Playwright-семантику timeout=0 (означает "без таймаута", т.е. бесконечное
    ожидание, а не мгновенную проверку). check_already_responded всегда должен
    получать полный _VISIBILITY_CHECK_TIMEOUT_MS, независимо от apply_button_found.
    """
    from hhru_bot.apply.dedup import _VISIBILITY_CHECK_TIMEOUT_MS

    for apply_button in (True, False):
        page = FakePage(apply_button=apply_button, already_responded=False)

        apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)

        assert page.already_responded_wait_for_timeouts
        assert all(
            t == _VISIBILITY_CHECK_TIMEOUT_MS for t in page.already_responded_wait_for_timeouts
        )


def test_apply_rechecks_responded_marker_before_form_submit():
    """A marker rendered during letter generation must block the submit.

    The recheck runs on the vacancy page (before navigating to the response
    form), so it can see vacancy-page markers. The marker appears only after
    the initial check, during letter render — the TOCTOU window #247 targets.
    The fake is URL-aware: vacancy-page markers exist only on the vacancy page,
    so the test fails if the recheck is ever moved after navigation (where the
    response-form DOM has no vacancy markers).
    """

    class _MarkerAppearsDuringLetterRender(FakePage):
        def wait_for_url(self, _url_pattern, **_kwargs):
            # navigate_to_response_form lands on the response-form page, where
            # vacancy-page markers are absent.
            self.url = "/applicant/vacancy_response"

        def locator(self, selector: str):
            if self.url.startswith("https://hh.ru/vacancy/"):
                return super().locator(selector)
            return _FakeLocator(present=False)

    page = _MarkerAppearsDuringLetterRender(apply_button=True, submit_in_form=True)

    class _LetterProvider:
        def render(self, _vacancy):
            page._already_responded = True
            return SimpleNamespace(text="x", variant="template")

    result = apply_to_vacancy(
        page, _vacancy(), "RID", "x", dry_run=False, letter_provider=_LetterProvider()
    )

    assert result.skipped is True
    assert result.skip_reason == SKIP_REASONS.ALREADY_APPLIED
    assert result.acted is False


def test_apply_already_responded_skip_reason_is_already_applied_not_has_questions():
    """#226 cycle-review round 2 (codex): already-responded skip раньше терялся под
    HAS_QUESTIONS — clear-skipped --reason already_applied не мог его снять, а
    clear-skipped --reason has_questions мог ошибочно пере-обработать уже
    откликнутую вакансию. Persistent-причина обязана быть ALREADY_APPLIED.
    """
    from hhru_bot.history import SKIP_REASONS

    page = FakePage(apply_button=False, already_responded=True)

    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True)

    assert result.skip_reason == SKIP_REASONS.ALREADY_APPLIED


def test_ctx_skip_default_reason_is_has_questions_unchanged():
    """#226 cycle-review round 2: skip_reason по умолчанию — HAS_QUESTIONS, как
    было единственное поведение questions-пути (#95, pipeline.py:245) до
    добавления явного skip_reason для already-responded-пути.
    """
    from hhru_bot.apply.pipeline import ApplyContext
    from hhru_bot.history import SKIP_REASONS

    ctx = ApplyContext(
        page=None, vacancy=_vacancy(), resume_id="RID", cover_letter_template="x", dry_run=True
    )

    result = ctx.skip("форма требует анкеты")

    assert result.skip_reason == SKIP_REASONS.HAS_QUESTIONS


def test_check_already_responded_ignores_hidden_attached_marker():
    """#226 cycle-review round 3 (codex, high): count()>0 проверял ТОЛЬКО attached
    к DOM, не видимость. Скрытая/устаревшая SPA-копия already-responded-маркера
    (шаблон, hidden-дубликат) была бы засчитана как «уже откликались» и записана
    persistent через record_skip(ALREADY_APPLIED) — почти необратимо исключая
    валидную вакансию из будущих прогонов. check_already_responded теперь требует
    ВИДИМОСТИ маркера (wait_for state='visible'), а не только count()>0.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from hhru_bot.apply.dedup import check_already_responded

    class _HiddenMarkerLocator:
        """Присутствует в DOM (count()==1), но не видим (wait_for(visible) падает)."""

        @property
        def first(self):
            return self

        def count(self) -> int:
            return 1

        def or_(self, _other):
            return self

        def filter(self, *, visible: bool | None = None):  # noqa: ARG002
            # A visible-only filter on a purely hidden marker yields no match —
            # model that as a locator whose wait_for always times out, so the
            # subsequent wait_for(state="attached") behaves like the real
            # filter(visible=True) narrowing to zero elements.
            return _HiddenMarkerLocator._EmptyLocator()

        class _EmptyLocator:
            @property
            def first(self):
                return self

            def wait_for(self, *, state: str = "attached", timeout: float = 0) -> None:  # noqa: ARG002
                raise PlaywrightTimeoutError("no visible marker")

        def wait_for(self, *, state: str = "attached", timeout: float = 0) -> None:  # noqa: ARG002
            if state == "visible":
                raise PlaywrightTimeoutError("hidden marker")

    class _HiddenMarkerPage:
        def locator(self, _selector: str) -> _HiddenMarkerLocator:
            return _HiddenMarkerLocator()

    reason = check_already_responded(_HiddenMarkerPage(), _vacancy())

    assert reason is None


def test_check_already_responded_visible_marker_survives_hidden_dom_order():
    """#248 cycle-review round 2 (codex, high): Locator.or_().first selects by
    DOM order, not by visibility. If a hidden/stale AGAIN marker precedes a
    visible CHAT marker in the union's DOM order, .first.wait_for(state=
    "visible") would wait on the hidden element and time out — even though a
    visible marker exists and proves an existing response. filter(visible=True)
    must be applied to the union before .first so a hidden element earlier in
    DOM order cannot hide a visible one later in DOM order.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from hhru_bot.apply.dedup import check_already_responded
    from hhru_bot.selector_groups import vacancy_page

    class _AgainMarkerLocator:
        """Hidden, and comes first in the union's DOM order."""

        @property
        def first(self):
            return self

        def or_(self, other):
            return _UnionLocator(other)

        def wait_for(self, *, state: str = "attached", timeout: float = 0) -> None:  # noqa: ARG002
            raise PlaywrightTimeoutError("hidden — never resolves as visible")

    class _ChatMarkerLocator:
        """Visible, but second in the union's DOM order."""

        @property
        def first(self):
            return self

        def wait_for(self, *, state: str = "attached", timeout: float = 0) -> None:  # noqa: ARG002
            return None

    class _UnionLocator:
        """Models Locator.or_(): .first picks by DOM order (AGAIN, i.e. first
        operand) unless narrowed by filter(visible=True) to only visible matches
        (here, only the CHAT operand)."""

        def __init__(self, chat_locator):
            self._chat = chat_locator

        @property
        def first(self):
            # Unfiltered union resolves .first to the hidden AGAIN marker
            # (earlier in DOM order) — this is the bug filter(visible=True) fixes.
            return _AgainMarkerLocator()

        def filter(self, *, visible: bool | None = None):  # noqa: ARG002
            # Narrowed to visible matches only: the hidden AGAIN marker drops
            # out, leaving the visible CHAT marker.
            return self._chat

    class _MarkerOrderPage:
        def locator(self, selector: str):
            if selector == vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT:
                return _ChatMarkerLocator()
            return _AgainMarkerLocator()

    reason = check_already_responded(_MarkerOrderPage(), _vacancy())

    assert reason == "уже откликались по вакансии 1, пропуск"


def test_wait_apply_button_already_responded_avoids_full_timeout():
    """#226 cycle-review: одна пара wait_for, не последовательное ожидание.

    wait_apply_button ждёт кнопку и already-responded-маркеры ОДНИМ объединённым
    локатором (Locator.or_) — если бы код сначала ждал полный APPLY_TIMEOUT_MS
    на кнопке и только потом проверял маркеры отдельным вызовом, здесь было бы
    больше одного wait_for на батч. На батче из многих already-responded вакансий
    последовательная схема копила бы по 10с задержки на каждую.
    """
    from hhru_bot.apply import steps as apply_steps

    page = FakePage(apply_button=False, already_responded=True)

    found = apply_steps.wait_apply_button(page)

    assert found is False  # кнопки нет — только already-responded маркер
    assert len(page.apply_wait_for_calls) == 1


def test_apply_probe_hook_invoked_noop_default():
    calls: list[str] = []

    # переопределяем __call__ через подкласс для наблюдения
    class Spy(ProbeHook):
        def __call__(self, stage: str, **kwargs):  # noqa: ARG002
            calls.append(stage)

    page = FakePage(apply_button=True)
    apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True, probe=Spy())  # type: ignore[arg-type]
    assert "vacancy_loaded" in calls


# --- #17: провайдер письма в pipeline ---


def test_apply_uses_letter_provider_when_given():
    # Прямая pipeline-интеграция: apply_to_vacancy(letter_provider=...) рендерит
    # письмо через провайдер (а не статичный .format), и ApplyResult несёт его
    # variant. Это точка подключения #17, отдельная от _common.run_apply_for_resume.
    from hhru_bot.apply.letter import LetterOutcome

    class _SpyProvider:
        def __init__(self):
            self.rendered_with = None

        def render(self, vacancy, resume_profile=None):  # noqa: ARG002
            self.rendered_with = vacancy.title
            return LetterOutcome(text="ai-letter-text", variant="ai")

    spy = _SpyProvider()
    page = FakePage(apply_button=True)
    result = apply_to_vacancy(
        page, _vacancy(), "RID", "IGNORED-TEMPLATE", dry_run=True, letter_provider=spy
    )
    assert result.success is True
    assert spy.rendered_with == "Dev"  # провайдер получил вакансию
    assert result.letter_variant == "ai"


def test_apply_letter_variant_template_without_provider():
    # Без провайдера variant остаётся 'template' (обратная совместимость).
    page = FakePage(apply_button=True)
    result = apply_to_vacancy(
        page, _vacancy(), "RID", "Hi {company_name}", dry_run=True, letter_provider=None
    )
    assert result.success is True
    assert result.letter_variant == "template"


def test_apply_letter_variant_preserved_on_fail():
    # fail() после рендера письма несёт variant провайдера (например, кнопка
    # отклика отсутствует — но это до рендера; проверяем путь с провайдером
    # и кнопкой нет → variant дефолт template, т.к. письмо не генерилось).
    page = FakePage(apply_button=False)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=True, letter_provider=None)
    assert result.success is False
    assert result.letter_variant == "template"


# --- #95 round-2: indeterminate form-scope не должен персиститься как skip ---


def test_apply_non_dry_run_success_when_submit_scoped_in_form():
    # non-dry-run путь доходит до detect_questions; когда submit корректно
    # обёрнут в <form> (обычный случай на реальном hh.ru), форма без вопросов
    # проходит как раньше — success, не regression от round-2 fix.
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is True
    assert result.skipped is False
    assert result.acted is True  # #163: submit выполнен — пауза обязательна


def test_apply_submit_unconfirmed_is_acted(monkeypatch):
    """#163: submit-клик был, но успех не подтвердился (wait_success_confirmation
    False) — это провал ПОСЛЕ действия: acted=True, цикл откликов обязан
    ждать паузу и писать failed. Регрессия против «фикс отключил троттлинг»."""
    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is False
    assert "не удалось подтвердить" in result.reason
    assert result.acted is True


def test_apply_non_dry_run_indeterminate_is_fail_not_skip():
    # #95 round-2 fix (Codex finding): если submit НЕ резолвится внутри <form>
    # (граница формы не определилась), detect_questions() возвращает
    # indeterminate — pipeline обязан трактовать это как fail (ApplyResult.skipped
    # остаётся False), а не как подтверждённый has_questions-skip. Иначе
    # неопределившийся scope навсегда пишется в permanent skip-кэш (#87) по
    # недостоверной причине — именно баг, который round-2 фикс устраняет.
    page = FakePage(apply_button=True, success=True, submit_in_form=False)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is False
    assert result.skipped is False
    assert "состояние формы" in result.reason
    assert result.acted is False  # #163: indeterminate — до submit, без паузы


def test_apply_form_render_failure_does_not_run_question_detection(monkeypatch):
    """A missing rendered form must not be misreported as a missing form scope."""
    page = FakePage(apply_button=True, success=False)
    detected = False
    monkeypatch.setattr(
        pipeline_module.apply_steps, "_dump_navigation_diagnostics", lambda *_args: None
    )

    def fail_if_detected(_page):
        nonlocal detected
        detected = True
        raise AssertionError("question detection must wait for confirmed form render")

    monkeypatch.setattr(pipeline_module, "detect_questions", fail_if_detected)

    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)

    assert result.success is False
    assert result.acted is False
    assert "форма отклика не отрисовалась" in result.reason
    assert detected is False


# --- #176: окно действия — исключение Playwright не теряет acted/запись --------


def test_apply_submit_click_error_is_uncertain_acted():
    """#176: Playwright упал в момент submit-клика — POST отклика мог уйти.
    Раньше исключение пробрасывалось из apply_to_vacancy: run_apply_for_resume
    не перехватывал его, цикл валился трейсбеком ДО record_action/throttle.wait,
    и отправленный (возможно) отклик выпадал из дедупликации has_applied.
    Fail-closed: результат с acted+uncertain — команда пишет 'uncertain' и
    ждёт паузу."""
    from playwright.sync_api import Error as PlaywrightError

    page = FakePage(
        apply_button=True,
        success=True,
        submit_in_form=True,
        submit_click_error=PlaywrightError("Target page, context or browser has been closed"),
    )
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "неопределён" in result.reason


def test_apply_confirmation_error_after_submit_keeps_acted(monkeypatch):
    """#177 round 3 (Codex): submit-клик прошёл, но wait_success_confirmation
    упал с PlaywrightError (не вернул False, а бросил). Это НЕ то же самое,
    что честный union-poll до таймаута без сигнала (result.uncertain=False,
    см. test_apply_submit_unconfirmed_is_acted — там мы ДОСТОВЕРНО проверили
    и не нашли успеха, осознанный fail-closed #163). Exception означает, что
    мы вообще не смогли проверить (browser/page упал посреди опроса) — тот же
    класс неопределённости, что и SubmitClickUncertain при самом клике.
    Поэтому acted=True И uncertain=True: дедупликация обязана отсечь
    вакансию, а не оставить её доступной для повторного отклика."""
    from playwright.sync_api import Error as PlaywrightError

    def _raise(_page):
        raise PlaywrightError("Page closed while polling success markers")

    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", _raise)
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "не подтверждён" in result.reason


def test_apply_prefill_playwright_error_is_clean_fail(monkeypatch):
    """#176: PlaywrightError из заполнения формы ДО submit (toggle/fill упали) —
    отправки не было, acted=False (без записи и паузы, как у ранних выходов
    #163), но traceback больше не рвёт цикл откликов: чистый fail-результат."""
    from playwright.sync_api import Error as PlaywrightError

    def _raise(*_a, **_kw):
        raise PlaywrightError("Element is not attached to the DOM")

    monkeypatch.setattr(pipeline_module.apply_steps, "fill_response_form", _raise)
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False)
    assert result.success is False
    assert result.acted is False
    assert result.uncertain is False
    assert "ошибка Playwright" in result.reason


# --- #207: внешняя верификация fail-вердиктов после клика по кнопке отклика ---


def _verifier(status: str, detail: str = ""):
    """Фейковый ResponseVerifier: фиксирует вызовы, возвращает заданный вердикт."""
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    calls: list[tuple] = []

    def verifier(page, vacancy_id, resume_id=None):  # noqa: ANN001
        calls.append((page, vacancy_id, resume_id))
        return NegotiationsVerifyResult(status, detail)

    verifier.calls = calls
    return verifier


def test_apply_submit_unconfirmed_external_found_is_success(monkeypatch):
    """#207 (кейс #199/МТС): submit был, успех не подтвердился локально, но
    внешний источник нашёл отклик в /applicant/negotiations — это success
    (acted=True, uncertain сброшен), а не failed: иначе has_applied не видит
    запись и следующий запуск шлёт второе письмо."""
    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)
    verifier = _verifier("found", "topic=42, resumeId=RID")
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is True
    assert result.acted is True
    assert result.uncertain is False
    assert "negotiations" in result.reason
    assert verifier.calls == [(page, "1", "RID")]


def test_apply_submit_unconfirmed_external_not_found_stays_failed(monkeypatch):
    """Подтверждённое внешней проверкой ОТСУТСТВИЕ отклика — вердикт не меняется:
    failed c acted=True (осознанный fail-closed #163, теперь ещё и проверенный)."""
    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)
    verifier = _verifier("not_found")
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is False
    assert "нет" in result.reason


def test_apply_submit_unconfirmed_external_indeterminate_is_uncertain(monkeypatch):
    """Список откликов не прочитан (goto/рендер/сессия) — прежний «честный failed»
    невозможен: исход неизвестен, fail-closed uncertain+acted как у #176."""
    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)
    verifier = _verifier("indeterminate", "goto не прошёл")
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "недоступна" in result.reason


def test_apply_form_indeterminate_external_found_is_success_acted():
    """#207 (кейс YADRO): форма не отрисовалась (questions-indeterminate), но
    отклик реально ушёл — внешняя проверка поднимает исход до success с
    acted=True: ранняя классификация «до submit, следов нет» здесь врала."""
    verifier = _verifier("found", "topic=9")
    page = FakePage(apply_button=True, success=True, submit_in_form=False)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is True
    assert result.acted is True
    assert result.uncertain is False
    assert "negotiations" in result.reason


def test_apply_form_indeterminate_external_not_found_keeps_early_exit():
    """Форма не отрисовалась И список подтверждённо без отклика — ранний выход
    сохраняется: acted=False, ничего не пишется в actions (следа на hh.ru нет)."""
    verifier = _verifier("not_found")
    page = FakePage(apply_button=True, success=True, submit_in_form=False)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is False
    assert result.acted is False
    assert result.uncertain is False


def test_apply_submit_click_error_external_found_upgrades_to_success():
    """#176+#207: исключение в момент submit-клика при найденном отклике —
    uncertain апгрейдится до success (внешний источник точнее локальной
    неопределённости)."""
    from playwright.sync_api import Error as PlaywrightError

    verifier = _verifier("found")
    page = FakePage(
        apply_button=True,
        success=True,
        submit_in_form=True,
        submit_click_error=PlaywrightError("Target page, context or browser has been closed"),
    )
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is True
    assert result.acted is True
    assert result.uncertain is False


def test_apply_confirmation_error_external_found_upgrades_to_success(monkeypatch):
    """#177+#207: PlaywrightError при подтверждении + найденный отклик —
    тоже апгрейд до success."""
    from playwright.sync_api import Error as PlaywrightError

    def _raise(_page):
        raise PlaywrightError("Page closed while polling success markers")

    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", _raise)
    verifier = _verifier("found")
    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=verifier)
    assert result.success is True
    assert result.acted is True
    assert result.uncertain is False


def test_apply_verifier_crash_is_uncertain_acted(monkeypatch):
    """#207: сбой самой внешней проверки (страница упала посреди опроса) не
    должен обрывать apply до записи в history и паузы троттлинга — иначе
    следующий запуск не увидит запись и отправит дубликат. Fail-closed:
    uncertain + acted, как у #176."""
    from playwright.sync_api import Error as PlaywrightError

    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)

    def _crash(page, vacancy_id, resume_id=None):  # noqa: ANN001
        raise PlaywrightError("Page closed while polling negotiations")

    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=_crash)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "упала" in result.reason


def test_apply_verifier_non_playwright_crash_is_uncertain_acted(monkeypatch):
    """#207: не-Playwright ошибка верификатора (ValueError из парсинга чужого
    SSR/DOM) — тот же класс неопределённости, что и упавшая страница: apply не
    должен оборваться до записи uncertain+acted (иначе дубликат на следующем
    запуске). Граница fail-closed ловит Exception, а не только PlaywrightError."""
    monkeypatch.setattr(pipeline_module, "wait_success_confirmation", lambda page: False)

    def _crash(page, vacancy_id, resume_id=None):  # noqa: ANN001
        raise ValueError("malformed href in topicList")

    page = FakePage(apply_button=True, success=True, submit_in_form=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "x", dry_run=False, verifier=_crash)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert "упала" in result.reason
