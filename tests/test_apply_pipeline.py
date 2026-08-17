"""Characterization-тесты apply/pipeline: оркестрация шагов.

Без браузера — через FakePage, имитирующий минимальный Playwright API,
используемый в шагах. Страхуют, что декомпозиция не изменила поведение
отклика (dry-run путь, уже откликались, кнопка не найдена, успех).
"""

from __future__ import annotations

import pytest

import hhru_bot.apply.pipeline as pipeline_module
from hhru_bot.apply import ProbeHook, apply_to_vacancy
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
    ):
        self._present = present
        self._attrs = attrs or {}
        # #176: PlaywrightError в момент click() (клик мог уйти на hh.ru).
        self._click_error = click_error

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
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

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.apply import success
        from hhru_bot.selector_groups import apply_form, vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=self._apply_button)
        if selector in (
            vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN,
            vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT,
        ):
            return _FakeLocator(present=self._already_responded)
        if selector == success.APPLY_SUCCESS_MARKER:
            return _FakeLocator(present=self._success)
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
    assert "границы формы" in result.reason
    assert result.acted is False  # #163: indeterminate — до submit, без паузы


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
