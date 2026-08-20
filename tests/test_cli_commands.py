"""Characterization-тесты CLI: авторегистрация команд и argparse-структура.

Без браузера — проверяем только build_parser/register_commands и presence
команд/аргументов. Страхует, что декомпозиция cli → commands/ не потеряла
команды и их флаги.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest

from hhru_bot.apply.antibot import AntiBotChallengeDetected, AntiBotDetection
from hhru_bot.cli import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HISTORY_PATH,
    _resolve_paths,
    build_parser,
    main,
    register_commands,
)
from hhru_bot.history import SKIP_REASONS, History

pytestmark = pytest.mark.integration


def _build() -> argparse.ArgumentParser:
    return build_parser()


def test_account_paths_are_defaults_but_explicit_paths_win(tmp_path, monkeypatch):
    account = tmp_path / "data" / "accounts" / "work"
    account.mkdir(parents=True)
    (account / "config.yaml").touch()
    parser = _build()

    monkeypatch.chdir(tmp_path)
    args = parser.parse_args(["--account", "work", "whoami"])
    _resolve_paths(args)
    assert Path(args.config).resolve() == account / "config.yaml"
    assert Path(args.history).resolve() == account / "history.db"

    args = parser.parse_args(
        [
            "--account",
            "missing-but-ignored",
            "--config",
            str(tmp_path / "custom.yaml"),
            "--history",
            str(tmp_path / "custom.db"),
            "whoami",
        ]
    )
    args.config = str(tmp_path / "custom.yaml")
    args.history = str(tmp_path / "custom.db")
    _resolve_paths(args)
    assert Path(args.config) == tmp_path / "custom.yaml"
    assert Path(args.history) == tmp_path / "custom.db"
    assert DEFAULT_CONFIG_PATH == Path("data/config.yaml")
    assert DEFAULT_HISTORY_PATH == Path("data/history.db")


def _subparser_actions(parser):
    # единственный subparsers action
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("subparsers не найден")


def test_all_commands_registered():
    parser = _build()
    action = _subparser_actions(parser)
    assert set(action.choices) == {
        "account",
        "login",
        "login-code",
        "search",
        "apply",
        "bump",
        "run",
        "probe",
        "stats",
        "schedule",
        "responses",
        "funnel",
        "mark",
        "query",
        "whoami",
        "list-resumes",
        "log",
        "clear-skipped",
        "skipped",
        "market",
        "copy-resume",
        "publish-resume",
        "edit-experience",
        "about",
        "import-cookies",
        "clear-negotiations",
        "delete-resume",
        "create-resume",
        "reply-employers",
        "calendar",
        "resume-position",
        "resume-sections",
        "edit-skills",
        "edit-education",
        "fill-form",
        "profile",
        "config",
        "call-api",
        "learn",
        "settings",
        "refresh-token",
        "reject",
    }


def test_register_commands_returns_names():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    names = register_commands(sub)
    # register_commands возвращает имена МОДУЛЕЙ (pkgutil module_info.name), а не
    # имена команд. Для log имя файла log_cmd.py (не log.py — конфликт stdlib),
    # поэтому модуль здесь — "log_cmd", хотя команда регистрируется как "log"
    # (проверяется отдельно в test_all_commands_registered через action.choices).
    # Аналогично модуль list_resumes регистрирует команду 'list-resumes'.
    assert set(names) == {
        "account",
        "login",
        "login_code",
        "search",
        "apply",
        "bump",
        "run",
        "probe",
        "stats",
        "schedule",
        "responses",
        "funnel",
        "mark",
        "query",
        "whoami",
        "list_resumes",
        "log_cmd",
        "clear_skipped",
        "skipped",
        "market",
        "copy_resume",
        "publish_resume",
        "edit_experience",
        "about",
        "import_cookies",
        "clear_negotiations",
        "delete_resume",
        "create_resume",
        "reply_employers",
        "calendar",
        "resume_position",
        "resume_sections",
        "edit_skills",
        "edit_education",
        "fill_form",
        "profile",
        "config_cmd",
        "call_api",
        "learn",
        "settings",
        "refresh_token",
        "reject",
    }


def _opts_for(command: str) -> set[str]:
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices[command]
    return {a.option_strings[0] for a in sub._actions if a.option_strings}


def test_search_has_common_args_no_limit():
    opts = _opts_for("search")
    assert "--resume" in opts
    assert "--dry-run" in opts
    assert "--max-pages" in opts
    assert "--limit" not in opts


def test_apply_has_limit():
    opts = _opts_for("apply")
    assert "--resume" in opts
    assert "--dry-run" in opts
    assert "--max-pages" in opts
    assert "--limit" in opts


def test_run_has_limit():
    opts = _opts_for("run")
    assert "--resume" in opts
    assert "--dry-run" in opts
    assert "--max-pages" in opts
    assert "--limit" in opts


def test_bump_no_limit():
    opts = _opts_for("bump")
    assert "--resume" in opts
    assert "--dry-run" in opts
    assert "--limit" not in opts


def test_probe_has_vacancy_args():
    opts = _opts_for("probe")
    assert "--resume" in opts
    assert "--vacancy-id" in opts
    assert "--vacancy-url" in opts
    # probe не откликается — дневной лимит/limit бессмысленны
    assert "--limit" not in opts


def test_schedule_has_generator_args():
    opts = _opts_for("schedule")
    assert "--format" in opts
    assert "--action" in opts
    assert "--bump-interval-hours" in opts
    assert "--apply-time" in opts
    assert "--apply-limit" in opts
    # schedule — генератор конфигов, не браузерная команда: общих поисковых
    # флагов и resume у неё нет (планировщик зовёт всё из config.yaml).
    assert "--resume" not in opts
    assert "--dry-run" not in opts
    assert "--max-pages" not in opts


def test_schedule_format_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["schedule"]
    fmt = next(a for a in sub._actions if "--format" in a.option_strings)
    assert set(fmt.choices) == {"plist", "crontab"}


def test_schedule_action_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["schedule"]
    act = next(a for a in sub._actions if "--action" in a.option_strings)
    assert set(act.choices) == {"bump", "apply"}


def test_login_no_common_args():
    opts = _opts_for("login")
    assert "--resume" not in opts
    assert "--dry-run" not in opts


def test_stats_has_period_and_format():
    opts = _opts_for("stats")
    assert "--resume" in opts
    assert "--period" in opts
    assert "--format" in opts
    # stats — не браузерная команда, общих поисковых флагов у неё нет
    assert "--dry-run" not in opts
    assert "--max-pages" not in opts


def test_stats_period_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["stats"]
    period = next(a for a in sub._actions if "--period" in a.option_strings)
    assert set(period.choices) == {"today", "week", "month", "all"}


def test_stats_format_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["stats"]
    fmt = next(a for a in sub._actions if "--format" in a.option_strings)
    assert set(fmt.choices) == {"table", "csv", "md"}


def test_responses_has_resume_max_pages_since_hours():
    opts = _opts_for("responses")
    assert "--resume" in opts
    assert "--max-pages" in opts
    assert "--since-hours" in opts
    # responses — read-only мониторинг: нет --dry-run (ничего не отправляет),
    # нет дневного лимита/--limit (не делает действий, подлежащих лимиту).
    assert "--dry-run" not in opts
    assert "--limit" not in opts


def test_funnel_has_format_and_dead_flags():
    opts = _opts_for("funnel")
    assert "--resume" in opts
    assert "--format" in opts
    assert "--dead" in opts
    assert "--dead-days" in opts
    # воронка — не браузерная команда
    assert "--dry-run" not in opts


def test_funnel_format_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["funnel"]
    fmt = next(a for a in sub._actions if "--format" in a.option_strings)
    # воронка — table/md (без csv, как stats #11)
    assert set(fmt.choices) == {"table", "md"}


def test_mark_requires_resume_and_vacancy():
    opts = _opts_for("mark")
    assert "--resume" in opts
    assert "--vacancy" in opts
    assert "--status" in opts


def test_mark_status_choices():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["mark"]
    status = next(a for a in sub._actions if "--status" in a.option_strings)
    assert set(status.choices) == {"offer"}


def test_whoami_has_resume_only():
    opts = _opts_for("whoami")
    assert "--resume" in opts
    assert "--online" in opts
    # READ-команда: ничего не отправляет и не делает действий под лимит —
    # --dry-run/--limit здесь бессмысленны (контракт спеки #21 §whoami).
    assert "--dry-run" not in opts
    assert "--limit" not in opts


def test_log_has_lines_and_follow():
    # _opts_for берёт option_strings[0] — у log флаги короткие: -n/-f.
    opts = _opts_for("log")
    assert "-n" in opts
    assert "-f" in opts
    # log — READ: ни резюме, ни dry-run/limit (не делает действий)
    assert "--resume" not in opts
    assert "--dry-run" not in opts
    assert "--limit" not in opts


def test_log_default_lines():
    parser = _build()
    action = _subparser_actions(parser)
    sub = action.choices["log"]
    lines = next(a for a in sub._actions if "--lines" in a.option_strings)
    assert lines.default == 50


def test_clear_skipped_success_does_not_exit_nonzero(tmp_path):
    """cli.main() must not treat clear-skipped's deleted-row count as failure.

    #148 made cli.main() call sys.exit(1) on any truthy command return, to
    fail closed on VacancySearchIndeterminate from search/apply/run. But
    clear_skipped.run() returns the number of deleted rows (int), not a bool
    success flag — a successful deletion of N>0 rows is truthy and must not
    be mistaken for an indeterminate-search failure.
    """
    history_path = tmp_path / "h.db"
    history = History(history_path)
    history.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)

    # main() doesn't sys.exit on success at all; if clear-skipped's positive
    # deleted-count is (mis)treated as failure, main() raises SystemExit(1).
    try:
        main(["--history", str(history_path), "clear-skipped"])
    except SystemExit as e:
        pytest.fail(f"main() exited with {e.code} on a successful deletion")


def test_unhandled_exception_from_command_is_logged_to_file(monkeypatch, capsys):
    """#179: необработанное исключение из args.func (напр. непойманный внутри
    apply-пайплайна Playwright TimeoutError) раньше уходило только в stderr
    Python'а — traceback не попадал в data/logs/hhru_bot.log, хотя setup_logging()
    к этому моменту уже настроил FileHandler. main() обязан записать полный
    traceback в файл перед тем, как перевыбросить исключение дальше (поведение
    для пользователя — тот же traceback в консоли + ненулевой exit — не меняется).

    LOG_DIR — module-level константа (см. logging_setup.py), вычисленная от cwd
    на момент импорта, поэтому тест не гоняет cwd, а читает файл там, где
    setup_logging() реально его создал.
    """
    from hhru_bot.logging_setup import LOG_DIR

    def _boom(_args: argparse.Namespace) -> bool:
        raise RuntimeError("simulated navigate_to_response_form crash (#179)")

    import hhru_bot.commands.whoami as whoami_module

    monkeypatch.setattr(whoami_module, "run", _boom)

    try:
        with pytest.raises(RuntimeError, match="simulated navigate_to_response_form crash"):
            main(["whoami"])

        log_file = LOG_DIR / "hhru_bot.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "Необработанное исключение в команде 'whoami'" in content
        assert "RuntimeError: simulated navigate_to_response_form crash (#179)" in content

        # #179 code-review round 2: логирование должно писать ТОЛЬКО в файл —
        # console_handler на "hhru_bot" (StreamHandler, stderr) не должен получить
        # эту запись, иначе Python допечатает тот же traceback после raise ещё
        # раз через excepthook, и пользователь увидит его дважды.
        stderr = capsys.readouterr().err
        assert "Необработанное исключение в команде 'whoami'" not in stderr
    finally:
        # main() настраивает FileHandler на hhru_bot заново при каждом вызове
        # (root.handlers.clear() внутри setup_logging), но если assert выше упадёт,
        # handler останется висеть на последующие тесты — чистим в любом случае.
        logging.getLogger("hhru_bot").handlers.clear()


def test_antibot_terminal_state_prints_fail_without_traceback(monkeypatch, capsys):
    """#344: terminal challenge stops the command with a human-readable failure."""

    def _challenge(_args: argparse.Namespace) -> bool:
        raise AntiBotChallengeDetected(
            AntiBotDetection("captcha_data_qa", "виден маркер captcha_data_qa")
        )

    import hhru_bot.commands.whoami as whoami_module

    monkeypatch.setattr(whoami_module, "run", _challenge)
    with pytest.raises(SystemExit) as exc_info:
        main(["whoami"])

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "[FAIL] обнаружена анти-бот проверка" in stderr
    assert "решите её вручную" in stderr
    assert "Traceback" not in stderr
    logging.getLogger("hhru_bot").handlers.clear()
