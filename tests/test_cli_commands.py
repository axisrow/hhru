"""Characterization-тесты CLI: авторегистрация команд и argparse-структура.

Без браузера — проверяем только build_parser/register_commands и presence
команд/аргументов. Страхует, что декомпозиция cli → commands/ не потеряла
команды и их флаги.
"""

from __future__ import annotations

import argparse

from hhru_bot.cli import build_parser, register_commands


def _build() -> argparse.ArgumentParser:
    return build_parser()


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
        "login",
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
        "market",
        "copy-resume",
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
        "login",
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
        "market",
        "copy_resume",
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
