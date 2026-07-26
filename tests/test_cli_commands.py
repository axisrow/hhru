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
    assert set(action.choices) == {"login", "search", "apply", "bump", "run", "probe"}


def test_register_commands_returns_names():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    names = register_commands(sub)
    assert set(names) == {"login", "search", "apply", "bump", "run", "probe"}


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


def test_login_no_common_args():
    opts = _opts_for("login")
    assert "--resume" not in opts
    assert "--dry-run" not in opts
