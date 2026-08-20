"""Политика троттлинга и записи истории «до действия» (#163).

#163: пауза ``throttle.wait`` и запись в ``actions`` — только после РЕАЛЬНОГО
действия на hh.ru (клик кнопки поднятия / submit отклика, ``acted=True``).
Dry-run ничего не отправляет и не должен считаться действием. Провалы до действия (плейсхолдер в конфиге,
форма входа, hint «рано», кнопка не найдена) не оставляют на hh.ru следа:
без паузы и без строки ``failed``. Прецедент — #95 (skip без отправки уже не
ждёт паузу и не пишется в actions).

Уровень командных циклов (``bump.run`` / ``run_apply_for_resume``): браузер и
поиск подменяются, ``Throttle.wait`` — шпион (реальный sleep в тестах недопустим),
History — настоящая в tmp_path, чтобы проверять, ЧТО реально попало в actions.
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.search import VacancyCard
from hhru_bot.throttle import LimitReached, Throttle

pytestmark = pytest.mark.integration


def _resume(**overrides) -> ResumeConfig:
    base = {
        "id": "python",
        "resume_url": "https://hh.ru/resume/AAA111",
        "search": SearchFilters(text="python developer"),
    }
    base.update(overrides)
    return ResumeConfig(**base)


def _placeholder_resume() -> ResumeConfig:
    # Тот же вид плейсхолдера, что в config.example.yaml (X{8,} в хвосте URL).
    return _resume(resume_url="https://hh.ru/resume/XXXXXXXXXXXXXXXXXXXXXXXX")


def _config(tmp_path, resume) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Здравствуйте, {company_name}!",
        resumes=[resume],
    )


def _read_actions(history_db) -> list[tuple[str, str]]:
    """(action, status) из actions в порядке записи."""
    conn = sqlite3.connect(history_db)
    try:
        return [tuple(r) for r in conn.execute("SELECT action, status FROM actions ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def wait_calls(monkeypatch):
    """Шпион на Throttle.wait: собирает причины вызовов вместо реального sleep."""
    calls: list[str] = []
    monkeypatch.setattr(Throttle, "wait", lambda self, reason="": calls.append(reason))
    return calls


# --- bump: командный цикл ----------------------------------------------------


class _FakeLaunchContext:
    """launch_context без браузера: page не используется подменёнными шагами."""

    def __enter__(self):
        class _Ctx:
            def new_page(self):
                return object()

        return _Ctx()

    def __exit__(self, *_a):  # noqa: ARG002
        return False


def _run_bump(monkeypatch, tmp_path, config, *, dry_run: bool = False) -> None:
    """bump.run с подменённым браузером/конфигом; throttle.wait — шпион фикстуры."""
    from hhru_bot.commands import bump as bump_cmd

    # String-path monkeypatch патчит символ в модуле-источнике: работает, потому
    # что bump.run импортирует launch_context/load_config_or_exit лениво.
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeLaunchContext())  # noqa: ARG005
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)  # noqa: ARG005
    bump_cmd.run(
        argparse.Namespace(
            config=None,
            history=str(tmp_path / "history.db"),
            dry_run=dry_run,
            headless=True,
            resume=None,
            max_pages=5,
        )
    )


def test_bump_placeholder_no_wait_no_record(tmp_path, monkeypatch, wait_calls, capsys):
    """Приёмка #163: dry-run с плейсхолдером — без 16-секундной паузы и без
    записи failed. Реальный bump_resume: плейсхолдер отсекается до goto_hh,
    на hh.ru нет ни одного запроса — пауза не от чего не защищает."""
    _run_bump(monkeypatch, tmp_path, _config(tmp_path, _placeholder_resume()), dry_run=True)

    assert wait_calls == [], "плейсхолдер не ходил на hh.ru — пауза не нужна"
    assert _read_actions(tmp_path / "history.db") == [], (
        "отсев по локальному конфигу — не действие на hh.ru"
    )
    assert "[FAIL]" in capsys.readouterr().out, "провал по-прежнему виден пользователю"


def test_bump_login_form_failure_no_wait_no_record(tmp_path, monkeypatch, wait_calls, capsys):
    """Форма входа (#153) — провал до клика: без паузы и без записи failed."""
    from hhru_bot.bump import BumpResult

    resume = _resume()
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda page, r, dry_run: BumpResult(  # noqa: ARG005
            r.id, False, "Сессия недействительна: страница содержит форму входа. Выполните login."
        ),
    )
    _run_bump(monkeypatch, tmp_path, _config(tmp_path, resume))

    assert wait_calls == []
    assert _read_actions(tmp_path / "history.db") == []
    assert "[FAIL]" in capsys.readouterr().out


def test_bump_real_action_waits_and_records(tmp_path, monkeypatch, wait_calls, capsys):
    """РЕГРЕССИЯ #163: после реального клика поднятия пауза обязательна и
    success пишется в actions — фикс не должен отключить троттлинг там,
    где он нужен (CLAUDE.md: «не убирай троттлинг/лимиты»)."""
    from hhru_bot.bump import BumpResult

    resume = _resume()
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda page, r, dry_run: BumpResult(r.id, True, "success", acted=True),  # noqa: ARG005
    )
    _run_bump(monkeypatch, tmp_path, _config(tmp_path, resume))

    assert wait_calls == [f"после поднятия резюме '{resume.id}'"]
    assert _read_actions(tmp_path / "history.db") == [("bump", "success")]
    assert "[OK]" in capsys.readouterr().out


def test_bump_dry_run_success_does_not_record_without_wait(tmp_path, monkeypatch, wait_calls):
    """Успешная dry-run-симуляция ничего не записывает и не ждёт паузу."""
    from hhru_bot.bump import BumpResult

    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda page, r, dry_run: BumpResult(r.id, True, "dry-run"),  # noqa: ARG005
    )
    _run_bump(monkeypatch, tmp_path, _config(tmp_path, _resume()), dry_run=True)

    assert wait_calls == []
    assert _read_actions(tmp_path / "history.db") == []


# --- bump: записи без действия не влияют на лимиты (п.2 ишью) ----------------


def test_non_success_rows_do_not_affect_limits(tmp_path):
    """count_today/can_bump_now считают только success: dry_run/failed строки
    не расходуют дневной лимит и не запускают кулдаун 4ч (проверка п.2 #163:
    «если запись оставляем, она не должна попадать в счётчики дневных лимитов»)."""
    from hhru_bot.history import History

    history = History(tmp_path / "history.db")
    history.record_action("AAA111", "AAA111", "bump", "failed", "кнопка не найдена")
    history.record_action("AAA111", "AAA111", "bump", "dry_run", "dry-run")

    throttle = Throttle(ThrottleConfig(), history)
    assert history.count_today("AAA111", "bump") == 0
    assert throttle.can_bump_now("AAA111") == (True, None)


def test_apply_limit_is_account_wide_across_resumes(tmp_path):
    """The daily apply allowance must not multiply with configured resumes."""
    from hhru_bot.history import History

    history = History(tmp_path / "history.db")
    history.record_action("AAA111", "1", "apply", "success")
    history.record_action("BBB222", "2", "apply", "uncertain")
    throttle = Throttle(ThrottleConfig(daily_apply_limit=2), history)

    with pytest.raises(LimitReached, match="account"):
        throttle.check_apply_limit("CCC333", dry_run=False)


# --- apply: командный цикл ---------------------------------------------------


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42")


def _run_apply(monkeypatch, tmp_path, *, dry_run: bool, result) -> None:
    """run_apply_for_resume с подменёнными поиском/откликом; throttle.wait — шпион."""
    from hhru_bot.commands import _common
    from hhru_bot.history import History

    resume = _resume()
    history = History(tmp_path / "history.db")
    throttle = Throttle(ThrottleConfig(), history)

    monkeypatch.setattr(_common, "search_vacancies", lambda page, search, max_pages=5: [_vacancy()])  # noqa: ARG005
    monkeypatch.setattr(
        _common,
        "apply_to_vacancy",
        lambda *a, **kw: result,  # noqa: ARG005
    )
    args = argparse.Namespace(dry_run=dry_run, limit=1, max_pages=5, headless=True)
    _common.run_apply_for_resume(
        object(), _config(tmp_path, resume), resume, history, throttle, args
    )


def test_apply_preaction_failure_no_wait_no_record(tmp_path, monkeypatch, wait_calls, capsys):
    """Провал до submit (форма входа и т.п.) — без паузы и без записи failed:
    отправки не было, на hh.ru не осталось следа действия."""
    from hhru_bot.apply import ApplyResult

    result = ApplyResult(
        _vacancy(), False, "Сессия недействительна: страница содержит форму входа."
    )
    _run_apply(monkeypatch, tmp_path, dry_run=False, result=result)

    assert wait_calls == []
    assert _read_actions(tmp_path / "history.db") == []
    assert "[FAIL]" in capsys.readouterr().out


def test_apply_submit_success_waits_and_records(tmp_path, monkeypatch, wait_calls):
    """РЕГРЕССИЯ #163: после реального submit пауза обязательна, success пишется."""
    from hhru_bot.apply import ApplyResult

    result = ApplyResult(_vacancy(), True, "success", acted=True)
    _run_apply(monkeypatch, tmp_path, dry_run=False, result=result)

    assert wait_calls == [f"после отклика на '{_vacancy().title}'"]
    assert _read_actions(tmp_path / "history.db") == [("apply", "success")]


def test_apply_submit_unconfirmed_waits_and_records_failed(tmp_path, monkeypatch, wait_calls):
    """Submit был, но успех не подтвердился (wait_success_confirmation) — это
    провал ПОСЛЕ действия: пауза обязательна, failed пишется в actions."""
    from hhru_bot.apply import ApplyResult

    result = ApplyResult(_vacancy(), False, "не удалось подтвердить отправку", acted=True)
    _run_apply(monkeypatch, tmp_path, dry_run=False, result=result)

    assert wait_calls == [f"после отклика на '{_vacancy().title}'"]
    assert _read_actions(tmp_path / "history.db") == [("apply", "failed")]


def test_apply_dry_run_does_not_record_or_deduplicate(tmp_path, monkeypatch, wait_calls):
    """Dry-run не отправляет отклик и не меняет локальную action-историю."""
    from hhru_bot.apply import ApplyResult
    from hhru_bot.history import History

    result = ApplyResult(_vacancy(), True, "dry-run")
    _run_apply(monkeypatch, tmp_path, dry_run=True, result=result)

    assert wait_calls == []
    assert _read_actions(tmp_path / "history.db") == []
    assert History(tmp_path / "history.db").has_applied("AAA111", "42") is False


# --- #176: uncertain — клик мог уйти, запись/пауза/лимиты обязательны ---------


def test_bump_uncertain_waits_and_records_uncertain(tmp_path, monkeypatch, wait_calls, capsys):
    """#176: Playwright упал в момент клика поднятия — действие могло уйти на
    hh.ru. Командный цикл обязан писать action (статус 'uncertain', НЕ 'failed'
    — его не видят кулдаун/лимит) и выдерживать анти-бан-паузу."""
    from hhru_bot.bump import BumpResult

    resume = _resume()
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda page, r, dry_run: BumpResult(  # noqa: ARG005
            r.id,
            False,
            "клик поднятия выполнен, исход неопределён",
            acted=True,
            uncertain=True,
        ),
    )
    _run_bump(monkeypatch, tmp_path, _config(tmp_path, resume))

    assert wait_calls == [f"после поднятия резюме '{resume.id}'"]
    assert _read_actions(tmp_path / "history.db") == [("bump", "uncertain")]
    assert "[FAIL]" in capsys.readouterr().out


def test_apply_uncertain_waits_and_records_uncertain(tmp_path, monkeypatch, wait_calls):
    """#176: submit-клик упал с исключением — отклик мог уйти. Запись
    'uncertain' (дедупликация has_applied его видит) + пауза, как минимум
    требование ишью: «гарантированно писать запись и выдерживать паузу»."""
    from hhru_bot.apply import ApplyResult
    from hhru_bot.history import History

    result = ApplyResult(
        _vacancy(), False, "submit-клик упал с исключением", acted=True, uncertain=True
    )

    # #441 round-2 review: uncertain — routine per-vacancy fail-closed outcome
    # (dedup via has_applied() below is what actually protects against a
    # duplicate application), not a genuine account-level terminal condition
    # — it must NOT raise ApplyRunStopped (that's reserved for stop_run).
    _run_apply(monkeypatch, tmp_path, dry_run=False, result=result)

    assert wait_calls == [f"после отклика на '{_vacancy().title}'"]
    assert _read_actions(tmp_path / "history.db") == [("apply", "uncertain")]
    # дедупликация отсечёт вакансию при повторном запуске — второго письма
    # работодателю не будет, даже если первый отклик действительно ушёл
    assert History(tmp_path / "history.db").has_applied("AAA111", "42") is True


def test_uncertain_rows_affect_limits_cooldown_and_dedup(tmp_path):
    """#176, антитеза test_non_success_rows_do_not_affect_limits: uncertain-строки
    fail-closed — расходуют дневной лимит (count_today), запускают кулдаун 4ч
    (can_bump_now через last_action_at) и дедуплицируют отклик. Действие могло
    выполниться на hh.ru — локальная история обязана считать его состоявшимся."""
    from hhru_bot.history import History

    history = History(tmp_path / "history.db")
    history.record_action("AAA111", "AAA111", "bump", "uncertain", "исход неопределён")
    history.record_action("AAA111", "42", "apply", "uncertain", "submit упал после клика")

    throttle = Throttle(ThrottleConfig(), history)
    assert history.count_today("AAA111", "bump") == 1
    can_bump, wait_left = throttle.can_bump_now("AAA111")
    assert can_bump is False
    assert wait_left is not None
    assert history.has_applied("AAA111", "42") is True
