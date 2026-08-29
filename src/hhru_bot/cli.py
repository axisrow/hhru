"""Точка входа CLI: skeleton build_parser с авторегистрацией команд + main().

Команды живут в пакете commands/, каждый модуль реализует register(subparsers).
build_parser обходит их через pkgutil.iter_modules и вызывает register. Новая
команда авторегистрируется без правок этого файла; если она запускает Chromium,
её дополнительно нужно классифицировать в BROWSER_COMMANDS (#568).
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import pkgutil
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import commands as _commands_pkg
from .accounts import AccountError, resolve_account_paths
from .apply.antibot import AntiBotChallengeDetected
from .browser import BrowserLaunchError, ThrottledChannelDetected
from .exit_codes import CommandExitCode
from .logging_setup import setup_logging
from .write_lock import WriteLockBusy, acquire_write_lock

# Дефолтные пути — ОТНОСИТЕЛЬНЫЕ (relative-to-cwd), а не привязанные к пакету.
# После `pip install` пакет уезжает в site-packages, и привязка путей к
# расположению кода (как раньше через PROJECT_ROOT = parents[2]) ломала бы поиск
# data/config.yaml. Относительные пути Python резолвит от cwd в рантайме —
# пользователь запускает `hhru-bot` из директории проекта, где рядом лежит
# data/. Относительные строки также стабильно смотрятся в --help и в
# автоген-справочнике README (gen_cli_docs.py), не завися от машины.
#
# Все изменяемые данные — под data/ (#133): конфиг, БД, сессия, логи. Вся папка
# целиком в .gitignore одной строкой.
DEFAULT_CONFIG_PATH = Path("data") / "config.yaml"
DEFAULT_HISTORY_PATH = Path("data") / "history.db"

# Commands that always start Playwright Chromium.  Keep conditional browser
# modes (list-resumes --local, whoami --online, and clear-negotiations plan
# modes) in _requires_browser().
# This registry lives at the CLI boundary deliberately: the Codex sandbox must
# be rejected before path resolution, logging, a write lock, History(), or a
# durable command_run can create local state (#568).
BROWSER_COMMANDS = frozenset(
    {
        "about",
        "apply",
        "bump",
        "call-api",
        "clear-negotiations",
        "competitors",
        "copy-resume",
        "create-resume",
        "delete-resume",
        "edit-education",
        "edit-experience",
        "edit-languages",
        "edit-skills",
        "fill-form",
        "login",
        "login-code",
        "probe",
        "professional-roles",
        "publish-resume",
        "refresh-token",
        "rename-resume",
        "reply-employers",
        "report-vacancy",
        "responses",
        "resume-position",
        "resume-sections",
        "resume-pool",
        "resume-visibility",
        "resume-views",
        "run",
        "search",
    }
)

WRITE_COMMANDS = frozenset(
    {
        "apply",
        "bump",
        "run",
        "copy-resume",
        "rename-resume",
        "publish-resume",
        "resume-visibility",
        "edit-experience",
        "about",
        "reply-employers",
        # responses only reads hh.ru, but it persists response rows and its
        # alert watermark; serialize those commits with the alert boundary.
        "responses",
        "edit-education",
        "clear-negotiations",
        "delete-resume",
        "create-resume",
        "resume-position",
        "resume-sections",
        "resume-pool",
        "edit-skills",
        "edit-languages",
        "settings",
        "config",
        "reject",
        "backup",
        "restore",
        "review",
        "blacklist",
        "update",
        "report-vacancy",
    }
)

# Nested commands need their own classification: account create mutates local
# files, while account list is a read-only directory scan.
WRITE_SUBCOMMANDS = frozenset(
    {
        ("account", "create"),
        ("competitors", "collect"),
        # #482: questionnaire set/unset/learn правят локальные шаблоны и очередь;
        # pending/templates только читают и должны оставаться доступными во время
        # идущего apply.
        ("questionnaire", "set"),
        ("questionnaire", "unset"),
        ("questionnaire", "learn"),
    }
)

# В каком атрибуте каждая вложенная команда хранит свою подкоманду. Раньше dest
# был захардкожен как ``account_command`` прямо в проверке ниже, поэтому любая
# новая вложенная WRITE-команда молча обходила бы write-lock: запись в
# WRITE_SUBCOMMANDS для неё просто никогда не совпадала бы (#482).
SUBCOMMAND_DESTS = {
    "account": "account_command",
    "competitors": "competitors_command",
    "questionnaire": "questionnaire_command",
}


def register_commands(subparsers: argparse._SubParsersAction) -> list[str]:
    """Обходит команды/ и вызывает register() у каждого модуля. Возвращает имена команд."""
    registered: list[str] = []
    for module_info in pkgutil.iter_modules(_commands_pkg.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{_commands_pkg.__name__}.{name}")
        if not hasattr(module, "register"):
            continue
        module.register(subparsers)
        registered.append(name)
    return registered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hhru_bot",
        description="Автоматизация поиска, откликов и поднятия резюме на hh.ru",
    )
    parser.add_argument("--config", help="Путь к config.yaml")
    parser.add_argument("--history", help="Путь к файлу истории (SQLite)")
    parser.add_argument(
        "--account",
        help="Имя аккаунта (data/accounts/<name>/config.yaml + history.db)",
    )
    parser.add_argument(
        "--headless", action="store_true", help="Запустить браузер в headless-режиме"
    )
    parser.add_argument("--verbose", action="store_true", help="Подробное логирование")
    parser.add_argument("--quiet", action="store_true", help="Не печатать поток прогресса")

    subparsers = parser.add_subparsers(dest="command", required=True)
    register_commands(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    raw_argv = sys.argv[1:] if argv is None else argv
    # A Windows console-script launcher cannot be replaced while it is the
    # active process. Re-exec before argparse/help, logging, or the write lock
    # so every form of the documented ``hhru update`` command starts from the
    # unlocked Python interpreter.
    if "update" in raw_argv:
        from .commands.update import _reexec_windows_launcher

        _reexec_windows_launcher()
    parser = build_parser()
    args = parser.parse_args(argv)
    _reject_sandboxed_browser_command(args)
    try:
        _resolve_paths(args)
    except AccountError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    if not _is_write_command(args):
        return _execute(args)

    lock_path = _write_lock_path(args)
    try:
        owner_command = args.command
        if args.command == "probe" and getattr(args, "questionnaires_only", False):
            owner_command += " --questionnaires-only"
        with acquire_write_lock(lock_path, command=owner_command):
            return _execute(args)
    except WriteLockBusy as exc:
        owner = exc.owner
        detail = (
            f" (pid={owner.get('pid')}, command={owner.get('command')}, "
            f"started_at={owner.get('started_at')})"
            if owner
            else ""
        )
        print(f"[FAIL] другой процесс уже выполняет WRITE-действие{detail}")
        sys.exit(1)


def _requires_browser(args: argparse.Namespace) -> bool:
    """Whether the parsed invocation will start Playwright Chromium."""
    if args.command == "list-resumes":
        return not getattr(args, "local", False)
    if args.command == "whoami":
        return bool(getattr(args, "online", False))
    if args.command == "clear-negotiations":
        # Filter-only plans and topic dry-runs return before config/History and
        # never open Chromium. Account-wide collection and a live topic
        # withdrawal do need the browser (#572 Codex review).
        return bool(
            getattr(args, "account_wide", False)
            or (getattr(args, "topic", None) and not getattr(args, "dry_run", False))
        )
    if args.command == "professional-roles":
        return bool(getattr(args, "refresh", False))
    if args.command == "competitors":
        return getattr(args, "competitors_command", None) == "collect"
    return args.command in BROWSER_COMMANDS


def _reject_sandboxed_browser_command(args: argparse.Namespace) -> None:
    """Fail before local state exists when Codex cannot launch Chromium.

    Codex sets ``CODEX_SANDBOX`` (currently ``seatbelt`` on macOS) only for the
    restricted execution path.  Elevated/system-permission commands receive an
    empty value.  ``CODEX_SANDBOX_NETWORK_DISABLED`` is intentionally ignored:
    it remains set on the permitted path and would reject valid launches.
    """
    if not os.environ.get("CODEX_SANDBOX") or not _requires_browser(args):
        return
    print(
        "[ENVIRONMENT] CODEX_SANDBOX_BROWSER_REQUIRED: "
        f"команда '{args.command}' запускает Chromium и должна быть сразу "
        "запущена вне sandbox с системным разрешением "
        "(sandbox_permissions=require_escalated). Браузерное действие, "
        "write-lock и durable run не начинались.",
        file=sys.stderr,
    )
    sys.exit(1)


def _is_write_command(args: argparse.Namespace) -> bool:
    """Whether this parsed command needs the write lock."""
    if args.command == "config":
        # Reading config must remain usable while an unrelated local write is
        # in progress.  The editor is included because it commits a mutation.
        return bool(args.set is not None or args.unset or args.edit)
    if args.command == "probe" and getattr(args, "questionnaires_only", False):
        return True
    if args.command == "professional-roles":
        return bool(getattr(args, "refresh", False))
    subcommand_dest = SUBCOMMAND_DESTS.get(args.command)
    subcommand = getattr(args, subcommand_dest, None) if subcommand_dest else None
    return (
        args.command in WRITE_COMMANDS
        or (args.command == "refresh-token" and getattr(args, "force", False))
        or (args.command, subcommand) in WRITE_SUBCOMMANDS
    )


def _write_lock_path(args: argparse.Namespace) -> Path:
    """Return the lock location for the state mutated by a write command."""
    if args.command == "professional-roles" and getattr(args, "refresh", False):
        from .professional_roles import DEFAULT_CACHE_PATH

        return DEFAULT_CACHE_PATH.expanduser().resolve().parent / ".professional_roles.lock"
    writes_config = args.command == "config" or getattr(args, "write_config", False)
    # copy-resume's post-click list diff is an account-wide reconciliation.
    # Serialize by the config/session identity even when callers intentionally
    # use separate history DBs (for example an isolated live-test audit).  A
    # history-scoped lock would let two processes clone the same source at once;
    # both new cards have the same parentResumeId, so either process could then
    # apply --title to the other's clone and persist the wrong resume id.
    # resume-pool (#754) performs the identical copy_resume_on_hh reconciliation
    # in a loop, once per missing cluster -- same account-wide race, same fix.
    mutates_external_resume_list = args.command in ("copy-resume", "resume-pool")
    lock_root = Path(args.config if writes_config or mutates_external_resume_list else args.history)
    return lock_root.expanduser().resolve().parent / ".hhru.lock"


def _resolve_paths(args: argparse.Namespace) -> None:
    """Apply account defaults while preserving explicit path arguments."""
    account_paths = None
    if args.account is not None and (args.config is None or args.history is None):
        account_paths = resolve_account_paths(args.account)
    args.config = str(
        Path(args.config)
        if args.config is not None
        else account_paths.config
        if account_paths is not None
        else DEFAULT_CONFIG_PATH
    )
    args.history = str(
        Path(args.history)
        if args.history is not None
        else account_paths.history
        if account_paths is not None
        else DEFAULT_HISTORY_PATH
    )
    # Keep the managed account directory separate from the user-controlled
    # config path.  A caller may combine --account with an explicit --config;
    # that config's parent must never be chmodded as if it were an account.
    args.account_dir = str(account_paths.config.parent) if account_paths else None


def _log_unhandled_to_file(command: str, message: str) -> None:
    """Write an unhandled-exception record to the hhru_bot FileHandler only.

    Shared by the generic ``except Exception`` path (#179) and the #747
    PlaywrightError branch below: both re-raise afterwards, letting Python's
    own excepthook print the traceback to stderr exactly once. Duplicating the
    console output here would print it twice.
    """
    record = logging.getLogger("hhru_bot").makeRecord(
        "hhru_bot",
        logging.ERROR,
        __file__,
        0,
        message,
        (command,),
        sys.exc_info(),
    )
    for handler in logging.getLogger("hhru_bot").handlers:
        if isinstance(handler, logging.FileHandler):
            handler.handle(record)


def _execute(args: argparse.Namespace) -> None:
    # READ-команда `log` намеренно минует setup_logging: FileHandler создал бы
    # data/logs/hhru_bot.log на запись до run(), что нарушает READ-контракт «не меняет
    # локально» (#21), делает ветку «файл не найден» недостижимой (setup_logging
    # создаёт пустой лог) и падает PermissionError в read-only-директории.
    # log сам ничего не логирует — ему не нужны handlers (цикл ревью #61, #58).
    # #179: то же условие решает, есть ли у логгера hhru_bot FileHandler — нужно
    # ниже ещё раз (except Exception), считаем один раз, не дублируем условие.
    logging_enabled = args.command not in {"log", "diagnostics"} and not (
        args.command == "account" and getattr(args, "account_command", None) == "list"
    )
    if logging_enabled:
        setup_logging(verbose=args.verbose)

    try:
        failed = args.func(args)
        # A command may return the conventional SIGINT status explicitly after
        # rendering a partial report (rather than raising KeyboardInterrupt).
        # Keep this separate from the bool-based fail-closed command contract.
        if isinstance(failed, CommandExitCode):
            sys.exit(failed.value)
        # Fail-closed contract (#148) is opt-in: only commands that report a
        # real bool success flag (search/apply/run) can trip sys.exit(1).
        # Commands returning other truthy values (e.g. clear-skipped's int
        # deleted-row count) or None must not be mistaken for a failure.
        if failed is True:
            sys.exit(1)
    except BrowserLaunchError as exc:
        print(f"[ENVIRONMENT] {exc}", file=sys.stderr)
        sys.exit(1)
    except PlaywrightError as exc:
        # #747: goto_hh (browser.py) пробрасывает PlaywrightTimeoutError/
        # PlaywrightError одинаково для любой причины — сеть недоступна вовсе
        # (DNS/connect timeout), анти-бот hh.ru или дрейф селектора/медленный
        # рендер. Различаем только то, что можно различить достоверно:
        # net::ERR_* — код уровня сетевого стека Chromium (Playwright не
        # добрался до ответа сервера вообще), а не таймаут рендера/анти-бота.
        if "net::ERR_" in str(exc):
            print(
                f"[ENVIRONMENT] похоже на отсутствие сети/соединения с hh.ru: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        # #749: третий класс — throttled-канал. goto_hh уже подтвердил (по
        # response навигационного запроса, полученному ДО таймаута), что
        # сервер ответил — TCP/TLS/заголовки прошли, узкое место строго в
        # скорости докачки тела. Отличается и от net::ERR_* (нет вовсе), и
        # от "чистого" TimeoutError без наблюдаемого response (там причина
        # остаётся неопределённой намеренно, см. ветку ниже).
        if isinstance(exc, ThrottledChannelDetected):
            print(
                f"[ENVIRONMENT] похоже на медленный/задушенный канал до hh.ru "
                f"(сервер ответил, но страница не докачалась): {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        # Остальные PlaywrightError (в т.ч. "чистый" TimeoutError без net::ERR_*
        # в сообщении — страница не отрисовалась, возможен анти-бот) НЕ
        # переквалифицируем: причина неизвестна, и по fail-closed инварианту
        # (CLAUDE.md §5) остаётся неопределённой. Тот же путь логирования в файл
        # + traceback, что и в except Exception ниже (#179).
        if logging_enabled:
            _log_unhandled_to_file(
                args.command,
                "Необработанное исключение в команде '%s' "
                "(страница не отрисовалась / возможен анти-бот)",
            )
        raise
    except AntiBotChallengeDetected as exc:
        # #344: terminal apply/run state.  Do not render a traceback or continue
        # with another vacancy/resume (or bump in the combined ``run`` command).
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(130)
    except Exception:
        # #179: раньше необработанное исключение из args.func (напр. Playwright
        # TimeoutError, не пойманный внутри pipeline) печаталось Python'ом только
        # в stderr — traceback не попадал в data/logs/hhru_bot.log, хотя
        # setup_logging() уже успел настроить FileHandler на этот момент.
        # SystemExit НЕ попадает сюда — он подкласс BaseException, не Exception
        # (sys.exit() из самой команды, напр. load_config_or_exit, пробрасывается
        # мимо этого except как раньше, не логируется как крах).
        if logging_enabled:
            # #179 code-review round 2: logger.exception() пишет в ОБА handler'а
            # (console + file, оба на "hhru_bot" — logging_setup.py), а следующий
            # bare raise даёт Python допечатать тот же traceback в stderr ещё раз
            # через excepthook — пользователь видел бы его дважды. Пишем запись
            # только в FileHandler напрямую, консоль получает traceback один раз
            # от самого Python (стандартное поведение необработанного исключения).
            _log_unhandled_to_file(args.command, "Необработанное исключение в команде '%s'")
        raise


if __name__ == "__main__":
    main()
