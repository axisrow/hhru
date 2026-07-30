"""Characterization-тесты probe-режима (#8): дамп формы отклика без отправки.

Без браузера — через FakePage, имитирующий минимальный Playwright API.
Главный инвариант: probe ДОХОДИТ до формы, заполняет письмо, дампит
screenshot + HTML в logs/, но submit_button.click() НИКОГДА не вызывается
(атомарность «дойти до формы и не отправить»).
"""

from __future__ import annotations

from pathlib import Path

from hhru_bot.apply.probe import ProbeContext, dump_probe_snapshot, probe_vacancy
from hhru_bot.search import VacancyCard


class _FakeLocator:
    """Локатор с отслеживанием click() — критично для проверки атомарности."""

    @property
    def first(self):
        return self

    def __init__(self, present: bool = False, attrs: dict[str, str] | None = None):
        self._present = present
        self._attrs = attrs or {}
        self.click_calls = 0
        self.fill_calls: list[str] = []

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "visible") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, **_kwargs) -> None:
        self.click_calls += 1

    def fill(self, value: str) -> None:
        self.fill_calls.append(value)

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def nth(self, _i: int) -> _FakeLocator:
        return self

    def locator(self, _selector: str) -> _FakeLocator:
        # Chained locator (#95 heuristic form-scope) — фейк не различает
        # вложенность формы, "внутри формы" считается пустым (0): тесты этого
        # файла не проверяют heuristic-содержимое, только сам факт
        # resolve/no-resolve form-scope через APPLY_SUBMIT_BUTTON.
        return _FakeLocator(present=False)


class _ClickTrackingLocator(_FakeLocator):
    """Локатор, сообщающий о каждом click() в общий счётчик владельца-страницы."""

    def __init__(self, present: bool, submit_clicks: list[int]):
        super().__init__(present=present)
        self._submit_clicks = submit_clicks

    def click(self, **_kwargs) -> None:
        self._submit_clicks.append(1)
        super().click()


class FakeProbePage:
    """Имитация Playwright Page для probe-пути. Отслеживает submit.click и дамп-методы."""

    def __init__(
        self,
        *,
        apply_button: bool = True,
        textarea: bool = True,
        submit: bool = True,
    ):
        self.url = ""
        self.goto_calls: list[str] = []
        self.screenshot_calls = 0
        self.content_calls = 0
        self._apply_button = apply_button
        self._textarea = textarea
        self._submit = submit
        # Список как мьютабельный счётчик: каждый click submit-локатора добавляет 1.
        self.submit_clicks: list[int] = []
        self._textarea_locator: _FakeLocator | None = None

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    def screenshot(self, **_kwargs) -> bytes:
        self.screenshot_calls += 1
        return b"\x89PNG probe-bytes"

    def content(self) -> str:
        self.content_calls += 1
        return "<html><body>probe dump</body></html>"

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.selector_groups import apply_form, vacancy_page

        # NOTE: check_already_responded — stub (всегда None) после #3; DOM-маркер
        # удалён из dedup, поэтому здесь нет ветки для него. probe не блокируется
        # дедупом (он идёт через history до apply_to_vacancy, а probe — диагностический
        # режим на конкретной вакансии).
        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=self._apply_button)
        if selector == apply_form.APPLY_COVER_LETTER_TEXTAREA:
            self._textarea_locator = _FakeLocator(present=self._textarea)
            return self._textarea_locator
        if selector == apply_form.APPLY_COVER_LETTER_TOGGLE:
            return _FakeLocator(present=False)
        if selector == f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]":
            # #95 round-2: submit обёрнут в <form> — used by detect_questions()
            # form-scope resolve. present=self._submit (та же готовность формы,
            # что и у самого submit — фейк не моделирует "submit есть, но вне
            # <form>" отдельно от готовности формы).
            return _FakeLocator(present=self._submit)
        if selector == apply_form.APPLY_SUBMIT_BUTTON:
            # Если probe вообще запросит submit — любой click будет зафиксирован.
            return _ClickTrackingLocator(present=self._submit, submit_clicks=self.submit_clicks)
        if selector == apply_form.APPLY_RESUME_SELECT:
            return _FakeLocator(present=False)
        return _FakeLocator(present=False)

    def expect_navigation(self, **_kwargs):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield

        return _cm()


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42")


# --- dump_probe_snapshot: пишет файлы в logs/ ---


def test_dump_probe_snapshot_writes_screenshot_and_html(tmp_path: Path):
    page = FakeProbePage()
    ctx = ProbeContext(
        vacancy_id="42",
        vacancy_url="https://hh.ru/vacancy/42",
        stage="form_filled",
        logs_dir=tmp_path,
    )

    paths = dump_probe_snapshot(page, ctx)

    assert page.screenshot_calls == 1
    assert page.content_calls == 1
    # файлы созданы и различимы по расширению
    written = {p.suffix for p in paths.values()}
    assert ".png" in written
    assert ".html" in written
    # содержимое записано
    png = [p for p in paths.values() if p.suffix == ".png"][0]
    html = [p for p in paths.values() if p.suffix == ".html"][0]
    assert png.read_bytes() == b"\x89PNG probe-bytes"
    assert "probe dump" in html.read_text(encoding="utf-8")
    # имя файла включает vacancy_id и stage для трассировки
    assert all("42" in p.name for p in paths.values())


# --- probe_vacancy: атомарность «не отправить» ---


def test_probe_does_not_click_submit(tmp_path: Path):
    page = FakeProbePage(apply_button=True, textarea=True, submit=True)

    result = probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Hi {company_name}",
        logs_dir=tmp_path,
    )

    assert result.success is True
    assert "probe" in result.reason.lower()
    # ГЛАВНЫЙ инвариант: submit никогда не кликается (даже если бы probe его запросил)
    assert page.submit_clicks == []
    # дошли до формы (навигация вызвана) и дамп сделали
    assert page.goto_calls == ["https://hh.ru/vacancy/42"]
    assert page.screenshot_calls >= 1
    assert page.content_calls >= 1


def test_probe_fills_cover_letter(tmp_path: Path):
    page = FakeProbePage(textarea=True)

    probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Здравствуйте, {company_name}",
        logs_dir=tmp_path,
    )

    assert page._textarea_locator is not None
    assert page._textarea_locator.fill_calls == ["Здравствуйте, Acme"]


def test_probe_no_apply_button_fails(tmp_path: Path):
    page = FakeProbePage(apply_button=False)

    result = probe_vacancy(page, _vacancy(), "RID", "x", tmp_path)

    assert result.success is False
    assert "кнопка отклика не найдена" in result.reason
    assert page.screenshot_calls == 0


def test_probe_dedup_step_is_passthrough(tmp_path: Path):
    # check_already_responded — stub (всегда None) после #3: DOM-маркер убран,
    # дедуп идёт через history до apply_to_vacancy. probe НЕ должен блокироваться
    # этим шагом — доходит до формы и дампит.
    page = FakeProbePage(apply_button=True)

    result = probe_vacancy(page, _vacancy(), "RID", "x", tmp_path)

    assert result.success is True
    assert page.screenshot_calls >= 1
    assert page.content_calls >= 1


# --- #17 (follow-up #54): AI-письмо в probe-дампе через letter_provider ---


class _SpyProvider:
    """Мок CoverLetterProvider для probe: фиксирует render-вызовы, возвращает
    заданный текст/variant. Не должен знать про textarea/submit — только генерация.
    """

    def __init__(self, text: str, variant: str = "ai"):
        self._text = text
        self._variant = variant
        self.render_calls: list[VacancyCard] = []

    def render(self, vacancy, resume_profile=None):  # noqa: ARG002
        from hhru_bot.apply.letter import LetterOutcome

        self.render_calls.append(vacancy)
        return LetterOutcome(text=self._text, variant=self._variant)


def test_probe_uses_letter_provider_text(tmp_path: Path):
    # Провайдер задан → textarea заполняется текстом от провайдера (AI под вакансию),
    # а НЕ статичным шаблоном. Главный контракт follow-up #54.
    page = FakeProbePage(textarea=True)
    provider = _SpyProvider(text="AI-письмо под Dev от Acme", variant="ai")

    probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Здравствуйте, {company_name}",
        logs_dir=tmp_path,
        letter_provider=provider,
    )

    assert provider.render_calls == [_vacancy()]
    assert page._textarea_locator is not None
    assert page._textarea_locator.fill_calls == ["AI-письмо под Dev от Acme"]


def test_probe_provider_does_not_click_submit(tmp_path: Path):
    # Атомарность probe сохраняется и с провайдером: submit не кликается.
    page = FakeProbePage(apply_button=True, textarea=True, submit=True)
    provider = _SpyProvider(text="AI письмо")

    probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Здравствуйте, {company_name}",
        logs_dir=tmp_path,
        letter_provider=provider,
    )

    assert page.submit_clicks == []


def test_probe_without_provider_uses_template(tmp_path: Path):
    # Характеризация обратной совместимости: без провайдера — статичный .format
    # (текст от шаблона, а не от провайдера).
    page = FakeProbePage(textarea=True)

    probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Здравствуйте, {company_name}",
        logs_dir=tmp_path,
        letter_provider=None,
    )

    assert page._textarea_locator is not None
    assert page._textarea_locator.fill_calls == ["Здравствуйте, Acme"]
