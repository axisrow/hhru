"""#165: плейсхолдер resume_url блокирует apply-цикл до любых действий.

PR #162 (issue #159) подключил is_resume_url_placeholder() в bump и probe,
но run_apply_for_resume плейсхолдер не проверял: если у аккаунта на hh.ru
одно резюме, форма отклика не рендерит селектор резюме и fill_response_form
отправляет отклик default-резюме, а история пишется под фейковым id
``XXXX...``. Ранний гард закрывает оба сценария из ишью (одно/несколько
резюме на аккаунте): до apply_to_vacancy дело не доходит вовсе.

Образцы — test_bump.py::test_bump_placeholder_url_does_not_navigate и
test_probe_healthcheck.py::test_check_selectors_skips_placeholder_url_without_goto.
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from hhru_bot.commands import _common
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.search import VacancyCard
from hhru_bot.throttle import Throttle

_PLACEHOLDER_URL = "https://hh.ru/resume/XXXXXXXXXXXXXXXXXXXXXXXX"


def _resume(url: str) -> ResumeConfig:
    return ResumeConfig(
        id="r1",
        resume_url=url,
        search=SearchFilters(text="python developer"),
    )


def _config(tmp_path, resume: ResumeConfig) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        # Нулевые задержки: контрольный тест доходит до throttle.wait.
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[resume],
    )


def _apply_args() -> argparse.Namespace:
    return argparse.Namespace(
        config=None,
        resume=None,
        dry_run=True,
        headless=True,
        max_pages=1,
        limit=0,
    )


def _row_count(history_db, table: str) -> int:
    with sqlite3.connect(history_db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class _StubResult:
    success = True
    reason = None
    letter_variant = None
    skipped = False


def test_placeholder_url_fails_closed_before_any_action(tmp_path, monkeypatch):
    """Плейсхолдер → fail closed до search/apply/истории, возврат True (exit 1)."""
    calls: list[str] = []

    def _forbidden(name):
        def _stub(*args, **kwargs):  # noqa: ARG001
            calls.append(name)
            pytest.fail(f"{name} must not be called with placeholder resume_url")

        return _stub

    monkeypatch.setattr(_common, "search_vacancies", _forbidden("search_vacancies"))
    monkeypatch.setattr(_common, "apply_to_vacancy", _forbidden("apply_to_vacancy"))

    history_db = tmp_path / "history.db"
    history = History(history_db)
    config = _config(tmp_path, _resume(_PLACEHOLDER_URL))
    throttle = Throttle(config.throttle, history)

    failed = _common.run_apply_for_resume(
        object(), config, config.resumes[0], history, throttle, _apply_args()
    )

    assert failed is True
    assert calls == []
    # История не пишется вовсе — в том числе под фейковым id XXXX...
    assert _row_count(history_db, "actions") == 0
    assert _row_count(history_db, "skipped") == 0


def test_real_url_still_reaches_apply(tmp_path, monkeypatch):
    """Обычный URL гард не ломает: цикл доходит до apply_to_vacancy."""
    monkeypatch.setattr(
        _common,
        "search_vacancies",
        lambda page, search, max_pages=1: [  # noqa: ARG001
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )
    applied_resume_ids: list[str] = []

    def _fake_apply(page, card, resume_id, template, dry_run, *, letter_provider=None):  # noqa: ARG001
        applied_resume_ids.append(resume_id)
        return _StubResult()

    monkeypatch.setattr(_common, "apply_to_vacancy", _fake_apply)

    history_db = tmp_path / "history.db"
    history = History(history_db)
    config = _config(tmp_path, _resume("https://hh.ru/resume/AAA111"))
    throttle = Throttle(config.throttle, history)

    failed = _common.run_apply_for_resume(
        object(), config, config.resumes[0], history, throttle, _apply_args()
    )

    assert failed is False
    assert applied_resume_ids == ["AAA111"]
