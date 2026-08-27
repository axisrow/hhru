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
from types import SimpleNamespace

import pytest

from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig

pytestmark = pytest.mark.integration


def _resume() -> ResumeConfig:
    """Резюме, у которого slug (id) и hh.ru-id (resume_id) заведомо различны."""
    return ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )


class _FakeLocator:
    """Минимальный Playwright-locator для apply-pipeline в dry-run."""

    @property
    def first(self):
        return self

    def __init__(self, present: bool = False):
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, *, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, *, timeout=None, no_wait_after=None) -> None:  # noqa: ARG002
        return None

    def fill(self, _value: str) -> None:  # noqa: ARG002
        return None

    def get_attribute(self, _name: str) -> str | None:  # noqa: ARG002
        return None

    def nth(self, _i: int) -> _FakeLocator:  # noqa: ARG002
        return self

    def or_(self, other: _FakeLocator) -> _FakeLocator:
        # #226 cycle-review: wait_apply_button() комбинирует apply-button и
        # already-responded-маркеры одним локатором.
        return _FakeLocator(present=self._present or other._present)

    def filter(self, *, visible: bool | None = None) -> _FakeLocator:  # noqa: ARG002
        # #248 cycle-review round 2: dedup.check_already_responded() narrows the
        # union to visible matches before .first — the fake has no hidden-vs-
        # visible distinction, so filtering is a no-op here.
        return self


class _ApplyFakePage:
    """Page для apply-pipeline: есть кнопка отклика.

    Dry-run стопает после кнопки отклика — до навигации на форму, поэтому submit
    не нужен. Дедуп-маркер «уже откликались» убран из pipeline (PR #27), поэтому
    locator() его больше не получает — check_already_responded всегда возвращает None.
    """

    def __init__(self):
        self.goto_calls: list[str] = []
        self.context = SimpleNamespace(
            cookies=lambda: [{"name": "hhtoken", "value": "test-session"}]
        )

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str):  # noqa: ARG002
        from hhru_bot.selector_groups import vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _FakeLocator(present=True)
        return _FakeLocator(present=False)

    def wait_for_url(self, _url_pattern, *, wait_until=None, timeout=None):  # noqa: ARG002
        # #179: navigate_to_response_form больше не использует expect_navigation.
        return None


def _read_resume_ids(history_db) -> list[tuple[str, str]]:
    """Список (resume_id, action) из actions в порядке записи."""
    import sqlite3

    conn = sqlite3.connect(history_db)
    try:
        rows = conn.execute("SELECT resume_id, action FROM actions ORDER BY id").fetchall()
    finally:
        conn.close()
    return rows


def test_apply_and_bump_dry_runs_do_not_record_actions(tmp_path, monkeypatch):
    """apply и bump dry-run не пишут действий без взаимодействия с hh.ru.

    Симметричный энд-ту-энд сценарий:
      1) apply (dry-run) через run_apply_for_resume — подмена только браузерного
         сбора карточек (search_vacancies), фейковый page, реальная History.
      2) bump (dry-run) через bump.run — подмена launch_context и bump_resume.
    Реальные submit/click-пути отдельно покрываются тестами записи успешных и
    неопределённых действий.
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

    # String-path monkeypatch патчит символ в модуле-источнике (browser/bump/config).
    # Работает только потому, что bump.run импортирует launch_context/bump_resume/
    # load_config_or_exit лениво (внутри функции) — каждый вызов подхватывает свежий патч.
    # Если импорты вынести наверх модуля bump.py — этот тест нужно править.
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
    assert rows == []
