"""Characterization-тесты probe-режима (#8): дамп формы отклика без отправки.

Без браузера — через FakePage, имитирующий минимальный Playwright API.
Главный инвариант: probe ДОХОДИТ до формы, заполняет письмо, дампит
screenshot + HTML в data/logs/, но submit_button.click() НИКОГДА не вызывается
(атомарность «дойти до формы и не отправить»).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hhru_bot.apply.probe as probe_module
from hhru_bot.apply.probe import ProbeContext, dump_probe_snapshot, probe_vacancy
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.integration


class _FakeLocator:
    """Локатор с отслеживанием click() — критично для проверки атомарности.

    ``render_delayed`` (#139): элемент присутствует, но ``count()`` БЕЗ
    предварительного ``wait_for`` видит пустой DOM (моделирует гонку рендера —
    поле ещё не отрисовалось в момент немедленного чтения). ``wait_for``
    всегда моделирует итоговое, дождавшееся состояние.
    """

    @property
    def first(self):
        return self

    def __init__(
        self,
        present: bool = False,
        attrs: dict[str, str] | None = None,
        *,
        render_delayed: bool = False,
        wait_sink: list[tuple[str, float]] | None = None,
    ):
        self._wait_sink = wait_sink
        self._present = present
        self._attrs = attrs or {}
        self.click_calls = 0
        self.fill_calls: list[str] = []
        self._render_delayed = render_delayed
        self._waited = False

    def count(self) -> int:
        if self._render_delayed and not self._waited:
            return 0
        return 1 if self._present else 0

    def wait_for(self, *, timeout: float = 0, state: str = "visible") -> None:
        self._waited = True
        if self._wait_sink is not None:
            self._wait_sink.append((state, timeout))
        if state == "hidden":
            # Ожидание СКРЫТИЯ выполняется, когда элемента нет (панель закрыта).
            # Обратная полярность: отсутствующий элемент — успех, а не таймаут.
            if self._present:
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                raise PlaywrightTimeoutError("still visible")
            return
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, *, timeout=None, no_wait_after=None) -> None:
        self.click_calls += 1

    def fill(self, value: str, *, timeout=None, no_wait_after=None, force=None) -> None:
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

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() объединяет кнопку и
        # already-responded-маркеры одним локатором.
        #
        # Письмо адресуется через or_ по двум shape формы (модалка/полная
        # страница). Реальный Playwright click()/fill() на or_-локаторе
        # действует на фактически совпавший элемент, поэтому возвращаем
        # присутствующий операнд как есть — иначе fill() уходил бы в новый
        # объект и тесты не видели бы заполненного письма.
        if self._present:
            return self
        if other._present:
            return other
        return _FakeLocator(present=False)

    def filter(self, *, visible: bool | None = None) -> _FakeLocator:  # noqa: ARG002
        # #248 cycle-review round 2: dedup.check_already_responded() narrows the
        # union to visible matches before .first — the fake has no hidden-vs-
        # visible distinction, so filtering is a no-op here.
        return self


class _ClickTrackingLocator(_FakeLocator):
    """Локатор, сообщающий о каждом click() в общий счётчик владельца-страницы."""

    def __init__(self, present: bool, submit_clicks: list[int]):
        super().__init__(present=present)
        self._submit_clicks = submit_clicks

    def click(self, *, timeout=None, no_wait_after=None) -> None:
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
        textarea_render_delayed: bool = False,
        resume_select: bool = True,
        resume_options: tuple[str, ...] = ("RID",),
        screenshot_error_from_call: int | None = None,
    ):
        # Дефолты моделируют ЖИВУЮ форму: `resume-title` присутствует всегда —
        # и на single-, и на multi-resume аккаунте (см. steps._select_resume_in_form).
        # Отсутствие селектора — аномалия, а не happy path: боевой fill_response_form
        # на нём отказывает, поэтому тесты happy path обязаны его иметь.
        self.url = ""
        # (state, timeout) всех wait_for по селектору выбора резюме: локатор
        # создаётся заново на каждый page.locator(), поэтому семантику ожидания
        # копим на уровне страницы.
        self.resume_select_waits: list[tuple[str, float]] = []
        # Номер первого screenshot-вызова (1-based), с которого страница
        # начинает бросать PlaywrightError. probe снимает несколько дампов
        # (form_initial, затем form), а сломать надо конкретный.
        self._screenshot_error_from_call = screenshot_error_from_call
        self._resume_select = resume_select
        self._resume_options = resume_options
        self.goto_calls: list[str] = []
        self.screenshot_calls = 0
        self.content_calls = 0
        self._apply_button = apply_button
        self._textarea = textarea
        self._submit = submit
        self._textarea_render_delayed = textarea_render_delayed
        # Список как мьютабельный счётчик: каждый click submit-локатора добавляет 1.
        self.submit_clicks: list[int] = []
        self._textarea_locator: _FakeLocator | None = None

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    def screenshot(self, *, full_page: bool | None = None, path=None) -> bytes:
        self.screenshot_calls += 1
        if (
            self._screenshot_error_from_call is not None
            and self.screenshot_calls >= self._screenshot_error_from_call
        ):
            from playwright.sync_api import Error as PlaywrightError

            raise PlaywrightError("Target page, context or browser has been closed")
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
            self._textarea_locator = _FakeLocator(
                present=self._textarea, render_delayed=self._textarea_render_delayed
            )
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
            return _FakeLocator(present=self._resume_select, wait_sink=self.resume_select_waits)
        if selector.startswith(f"[data-qa='{apply_form.APPLY_RESUME_OPTION_PREFIX}"):
            resume_id = selector.split(apply_form.APPLY_RESUME_OPTION_PREFIX, 1)[1].rstrip("']")
            return _FakeLocator(present=resume_id in self._resume_options)
        if selector == apply_form.APPLY_RESUME_DROPDOWN:
            # Панель закрыта — тесты этого файла не моделируют её залипание.
            return _FakeLocator(present=False)
        return _FakeLocator(present=False)

    def wait_for_url(self, _url_pattern, *, wait_until=None, timeout=None):
        # #179: navigate_to_response_form больше не использует expect_navigation.
        return None


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42")


# --- dump_probe_snapshot: пишет файлы в data/logs/ ---


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
    # #340: исходный DOM сохраняется до попытки выбора резюме/заполнения письма.
    assert (tmp_path / "probe_42_form_initial.html").exists()


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


def test_probe_reports_missing_letter_field_and_still_dumps(tmp_path: Path):
    """Отказ заполнить письмо не должен выглядеть как успешный probe.

    probe существует, чтобы воспроизводить боевой путь: fill_cover_letter —
    fail-closed (без textarea боевой apply отменяет отправку), и в докстринге
    самого probe (#139) зафиксировано, что «дамп выглядел валидным, хотя письмо
    не заполнено» — это ложная уверенность перед боевым запуском. Дамп при этом
    обязателен: он и есть диагностический артефакт.
    """
    page = FakeProbePage(textarea=False)

    result = probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)

    assert result.success is False
    assert result.skipped is False
    assert "письм" in result.reason.lower()
    # Дамп сохранён несмотря на отказ — иначе диагностировать нечего.
    assert result.dump_paths
    assert (tmp_path / "probe_42_form.html").exists()


def test_probe_reports_resume_selection_failure(tmp_path: Path):
    """Резюме не найдено среди опций — боевой apply отказал бы до submit.

    probe обязан сообщить тот же отказ, а не печатать [OK]: иначе диагностика
    даёт ложное подтверждение ровно перед боевым прогоном.
    """
    page = FakeProbePage(textarea=True, resume_select=True, resume_options=())

    result = probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)

    assert result.success is False
    assert result.skipped is False
    assert "резюме" in result.reason.lower()
    assert result.dump_paths


def test_probe_missing_resume_select_is_fail_closed(tmp_path: Path):
    """Селектора выбора резюме нет в форме — боевой apply отменяет отправку.

    Раньше probe в этом случае молча пропускал выбор резюме (гейт `_is_visible`
    с коротким OPTIONAL_FIELD_TIMEOUT_MS) и печатал [OK] — ложное подтверждение
    прямо перед боевым прогоном. probe обязан выдать тот же fail-closed вердикт,
    что и fill_response_form.
    """
    page = FakeProbePage(textarea=True, resume_select=False)

    result = probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)

    assert result.success is False
    assert result.skipped is False
    assert "резюме" in result.reason.lower()
    # Дамп всё равно сохранён — он и есть диагностический артефакт.
    assert result.dump_paths


def test_probe_waits_resume_select_with_live_path_semantics(tmp_path: Path):
    """probe ждёт селектор резюме ровно как боевой путь: attached + 10с.

    Расхождение семантики (visible/1.5с в probe против attached/10с в бою) —
    ровно то, из-за чего probe печатал [OK] при медленном рендере формы.
    """
    from hhru_bot.apply.steps import RESUME_SELECT_TIMEOUT_MS

    page = FakeProbePage(textarea=True)

    probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)

    assert ("attached", RESUME_SELECT_TIMEOUT_MS) in page.resume_select_waits


def test_probe_failure_dump_is_best_effort(tmp_path: Path):
    """Сбой артефакта на fail-пути не должен прятать сам вердикт.

    PlaywrightError на screenshot достижим (target closed/detached после
    неудачного взаимодействия с формой). Строгий режим дампа пробрасывал бы
    исключение наружу — CLI не напечатал бы [FAIL], а частично снятые пути
    потерялись бы вместе с результатом.
    """
    # Первый дамп (form_initial) снимается штатно; ломается второй — итоговый,
    # тот самый, который сопровождает вердикт отказа.
    page = FakeProbePage(textarea=False, screenshot_error_from_call=2)

    result = probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)

    assert result.success is False
    assert "письм" in result.reason.lower()
    # Частичный артефакт сохранён: HTML снялся, screenshot — нет.
    assert "html" in result.dump_paths
    assert "screenshot" not in result.dump_paths


def test_probe_success_dump_stays_strict(tmp_path: Path):
    """Успешный путь остаётся строгим: 'probe dump saved' без дампа — ложь.

    Здесь best-effort недопустим: probe заявляет готовый артефакт для сверки
    селекторов, и молчаливое [OK] без файлов вернуло бы ровно ту ложную
    уверенность, ради устранения которой probe и существует.
    """
    from playwright.sync_api import Error as PlaywrightError

    page = FakeProbePage(textarea=True, screenshot_error_from_call=2)

    with pytest.raises(PlaywrightError):
        probe_vacancy(page, _vacancy(), "RID", "письмо", tmp_path)


def test_probe_no_apply_button_fails(tmp_path: Path):
    page = FakeProbePage(apply_button=False)

    result = probe_vacancy(page, _vacancy(), "RID", "x", tmp_path)

    assert result.success is False
    assert "кнопка отклика не найдена" in result.reason
    assert page.screenshot_calls == 0


def test_probe_indeterminate_saves_partial_dump(tmp_path: Path):
    """A form-scope timeout leaves a diagnostic DOM artifact when possible."""
    page = FakeProbePage(submit=False)

    result = probe_vacancy(page, _vacancy(), "RID", "x", tmp_path)

    assert result.success is False
    assert result.skipped is True
    assert "форма отклика не отрисовалась" in result.reason
    partial_html = tmp_path / "probe_42_form_indeterminate.html"
    assert partial_html.exists()
    assert "probe dump" in partial_html.read_text(encoding="utf-8")


def test_probe_login_form_is_checked_after_navigation(tmp_path: Path, monkeypatch):
    page = FakeProbePage()
    events: list[str] = []

    def fake_goto(p, url, **_kwargs):
        events.append("goto")
        p.goto(url)

    def fake_has_login_form(_page):
        events.append("auth")
        assert events == ["goto", "auth"]
        return True

    monkeypatch.setattr(probe_module, "goto_hh", fake_goto)
    monkeypatch.setattr(probe_module, "has_login_form", fake_has_login_form)

    result = probe_vacancy(page, _vacancy(), "RID", "x", tmp_path)

    assert result.success is False
    assert "Сессия недействительна" in result.reason
    assert events == ["goto", "auth"]


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


# --- #139: гонка рендера — письмо заполняется через bounded wait, не голый count() ---


def test_probe_delayed_textarea_still_gets_filled(tmp_path: Path):
    """РЕГРЕССИЯ #139: textarea письма рендерится не мгновенно (гонка рендера).
    Немедленный ``count()`` без ожидания видит 0 — старый код молча пропускал
    заполнение, дамп выглядел валидным при незаполненном письме. probe обязан
    дождаться (bounded wait_for), а не читать count() сразу."""
    page = FakeProbePage(textarea=True, textarea_render_delayed=True)

    probe_vacancy(
        page,
        _vacancy(),
        resume_id="RID",
        cover_letter_template="Здравствуйте, {company_name}",
        logs_dir=tmp_path,
    )

    assert page._textarea_locator is not None
    assert page._textarea_locator.fill_calls == ["Здравствуйте, Acme"]


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
