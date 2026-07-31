"""Characterization-тесты apply/pipeline: оркестрация шагов.

Без браузера — через FakePage, имитирующий минимальный Playwright API,
используемый в шагах. Страхуют, что декомпозиция не изменила поведение
отклика (dry-run путь, уже откликались, кнопка не найдена, успех).
"""

from __future__ import annotations

from hhru_bot.apply import ProbeHook, apply_to_vacancy
from hhru_bot.search import VacancyCard


class _FakeLocator:
    @property
    def first(self):
        return self

    def __init__(self, present: bool = False, attrs: dict[str, str] | None = None):
        self._present = present
        self._attrs = attrs or {}

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, **_kwargs) -> None:
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
        success: bool = True,
        submit_in_form: bool = False,
    ):
        self.url = ""
        self.goto_calls: list[str] = []
        self._apply_button = apply_button
        self._success = success
        # #95 round-2: submit обёрнут в <form> (детектится xpath=ancestor::form[1]
        # в apply/questions.py::_form_scope) — по умолчанию False, чтобы явно
        # моделировать indeterminate-путь там, где тест его не настраивает.
        self._submit_in_form = submit_in_form

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.apply import success
        from hhru_bot.selector_groups import apply_form, vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=self._apply_button)
        if selector == success.APPLY_SUCCESS_MARKER:
            return _FakeLocator(present=self._success)
        if selector == f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]":
            return _FakeLocator(present=self._success and self._submit_in_form)
        # Прочие селекторы формы — считаем отсутствующими (форма не заполнена,
        # но submit присутствует в фейковом успехе через success-путь ниже).
        if selector == apply_form.APPLY_SUBMIT_BUTTON:
            return _FakeLocator(present=self._success)
        return _FakeLocator(present=False)

    def expect_navigation(self, **_kwargs):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield

        return _cm()


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id="1", title="Dev", company="Acme", url="https://hh.ru/vacancy/1")


# --- dry-run ---


def test_apply_dry_run_success():
    page = FakePage(apply_button=True)
    result = apply_to_vacancy(page, _vacancy(), "RID", "Здравствуйте, {company_name}", dry_run=True)
    assert result.success is True
    assert result.reason == "dry-run"
    assert page.goto_calls == ["https://hh.ru/vacancy/1"]


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
