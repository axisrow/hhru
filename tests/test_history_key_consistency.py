"""Тест на согласованность ключа истории apply ↔ bump (#2).

Баг: apply-путь пишет/читает actions по ``resume.id`` (человекочитаемый slug из
конфига), а bump-путь — по ``resume.resume_id`` (число из URL). Колонка
``actions.resume_id`` смешивает два пространства ключей, поэтому ``has_applied``,
``count_today`` и ``can_bump_now`` живут в разных ключевых пространствах и не
видят записи другого пути.

Критерий готовности ишью #2: единый ключ ``resume.resume_id`` во всех записях
``actions``.

Подход — characterization через реальные функции команд, а не хардкод ключей:
запускаем ``run_apply_for_resume`` (apply) и ``bump.run`` (bump) с подменами
браузера/поиска и реальной ``History``, после чего читаем ``resume_id`` прямо из
``actions``. На бажном коде apply пишет под ``resume.id`` (slug ``python``),
bump — под ``resume.resume_id`` (``AAA111``) → ключи расходятся → тест падает.
После unify оба пишут под ``resume.resume_id`` → тест зелёный.
"""

from __future__ import annotations

import argparse

from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig


def _resume() -> ResumeConfig:
    """Резюме, у которого slug (id) и hh.ru-id (resume_id) заведомо различны."""
    return ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )


class _FakeLocator:
    """Минимальный Playwright-locator для apply-pipeline в dry-run."""

    def __init__(self, present: bool = False):
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0) -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self) -> None:
        return None

    def fill(self, _value: str) -> None:  # noqa: ARG002
        return None

    def get_attribute(self, _name: str) -> str | None:  # noqa: ARG002
        return None

    def nth(self, _i: int) -> _FakeLocator:  # noqa: ARG002
        return self


class _ApplyFakePage:
    """Page для apply-pipeline: есть кнопка отклика, нет маркера «уже откликались».

    Dry-run стопает после кнопки отклика — до навигации на форму, поэтому submit
    не нужен.
    """

    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.apply import dedup
        from hhru_bot.selector_groups import vacancy_page

        if selector == dedup.APPLY_ALREADY_RESPONDED_MARKER:
            return _FakeLocator(present=False)
        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=True)
        return _FakeLocator(present=False)

    def expect_navigation(self, **_kwargs):  # noqa: ARG002
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield

        return _cm()


def _read_resume_ids(history_db) -> list[tuple[str, str]]:
    """Список (resume_id, action) из actions в порядке записи."""
    import sqlite3

    conn = sqlite3.connect(history_db)
    try:
        rows = conn.execute("SELECT resume_id, action FROM actions ORDER BY id").fetchall()
    finally:
        conn.close()
    return rows


def test_apply_and_bump_record_under_same_resume_key(tmp_path, monkeypatch):
    """apply-путь и bump-путь пишут в actions под одним resume_id = resume.resume_id.

    Симметричный энд-ту-энд сценарий:
      1) apply (dry-run) через run_apply_for_resume — подмена только браузерного
         сбора карточек (search_vacancies), фейковый page, реальная History.
      2) bump (dry-run) через bump.run — подмена launch_context и bump_resume.
    После обоих шагов resume_id в actions должен быть единым и равен
    resume.resume_id. На бажном коде оба пути пишут slug 'python' (через
    resume.id) вместо числового resume.resume_id='AAA111' — это и есть симптом
    бага: ключ истории завязан на переименуемый slug, а не на стабильный id hh.ru.
    """
    from hhru_bot.bump import BumpResult
    from hhru_bot.commands import _common
    from hhru_bot.commands import bump as bump_cmd
    from hhru_bot.history import History
    from hhru_bot.search import VacancyCard
    from hhru_bot.throttle import Throttle

    resume = _resume()
    history_db = tmp_path / "history.db"
    history = History(history_db)
    throttle = Throttle(ThrottleConfig(), history)
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="письмо {vacancy_title}",
        resumes=[resume],
    )

    # --- apply-путь (dry-run) ---
    card = VacancyCard(vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42")
    monkeypatch.setattr(_common, "search_vacancies", lambda page, search, max_pages=5: [card])  # noqa: ARG005
    apply_args = argparse.Namespace(dry_run=True, limit=1, max_pages=5, headless=True)
    _common.run_apply_for_resume(_ApplyFakePage(), config, resume, history, throttle, apply_args)

    # --- bump-путь (dry-run) ---
    class _CtxManager:
        def __enter__(self):
            class _Ctx:
                def new_page(self):
                    return object()

            return _Ctx()

        def __exit__(self, *_a):  # noqa: ARG002
            return False

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _CtxManager())  # noqa: ARG005
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda page, r, dry_run: BumpResult(resume_id=r.id, success=True, reason="dry-run"),  # noqa: ARG005
    )
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)  # noqa: ARG005
    bump_cmd.run(
        argparse.Namespace(
            config=None,
            history=str(history_db),
            dry_run=True,
            headless=True,
            resume=None,
            max_pages=5,
        )
    )

    rows = _read_resume_ids(history_db)
    apply_rows = [r for r in rows if r[1] == "apply"]
    bump_rows = [r for r in rows if r[1] == "bump"]
    assert apply_rows, "apply-путь ничего не записал в историю"
    assert bump_rows, "bump-путь ничего не записал в историю"

    apply_key = apply_rows[0][0]
    bump_key = bump_rows[0][0]
    assert apply_key == bump_key == resume.resume_id, (
        "apply и bump должны писать в историю под resume.resume_id="
        f"{resume.resume_id!r}, но apply={apply_key!r}, bump={bump_key!r}"
    )

    # Гарантируем, что тест осмысленен: slug и resume_id различны по построению,
    # иначе равенство ключей доказывало бы мало.
    assert resume.id != resume.resume_id
