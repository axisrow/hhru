"""Команда schedule (#18): генератор конфигов внешнего планировщика ОС.

Этап 6 «автопилот»: регулярный запуск hhru-bot через системный планировщик
(launchd на macOS / cron на Linux). CLAUDE.md ЗАПРЕЩАЕТ фоновые демоны внутри
самого проекта — поэтому эта команда ничего не демонизирует и не запускает
никаких фоновых процессов. Она лишь ПЕЧАТАЕТ готовый конфиг (.plist для launchd
или crонтаб-строку для cron) для копирования пользователем в свой планировщик.

Безопасность обеспечивается самим кодом hhru-bot, а не планировщиком:
предохранители в throttle.py (check_apply_limit — дневной лимит откликов,
can_bump_now — кулдаун поднятия 4 часа) не дадут переоткликнуться или
поднять резюме раньше срока, даже если планировщик «нажмёт кнопку» дважды.
Планировщик лишь вызывает scripts/scheduled_run.sh в нужное время.

Конфиги — ШАБЛОНЫ: реальные пути (к репозиторию, к logs/) пользователь
подставляет сам под свою машину. В .plist используются плейсхолдеры вида
__REPO_ROOT__/scripts/scheduled_run.sh, которые нужно заменить перед
установкой агента.

Команда авторегистрируется через pkgutil.iter_modules в cli.register_commands
(cli.py не трогается).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

VALID_ACTIONS = ("bump", "apply")
VALID_FORMATS = ("plist", "crontab")

# Шаблонные плейсхолдеры — НЕ реальные пути. Пользователь заменяет их под свою
# машину перед установкой LaunchAgent / crontab-записи. Так шаблоны безопасно
# коммитятся без раскрытия чьей-либо файловой системы.
PLACEHOLDER_REPO_ROOT = "__REPO_ROOT__"
PLACEHOLDER_LOG_DIR = "__LOG_DIR__"
# Абсолютный путь к python из venv проекта (напр. __REPO_ROOT__/.venv/bin/python).
# launchd/cron не активируют venv и имеют урезанный PATH — голый python3
# резолвится в системный без playwright. scheduled_run.sh читает HHRU_PYTHON.
PLACEHOLDER_PYTHON_BIN = "__PYTHON_BIN__"

# Дефолты повторяют логику авторегулярного запуска: bump не чаще 4ч (равен
# BUMP_COOLDOWN в throttle.py, чтобы запуск приходился на границу кулдауна),
# apply раз в день утром.
DEFAULT_BUMP_INTERVAL_HOURS = 4
DEFAULT_APPLY_TIME = "10:00"
DEFAULT_APPLY_LIMIT = 5


@dataclass
class ScheduleConfig:
    """Параметры генерируемого конфига планировщика."""

    format: str
    action: str
    interval_hours: int
    apply_time: str
    apply_limit: int

    def validate(self) -> None:
        if self.format not in VALID_FORMATS:
            raise ValueError(f"Неизвестный формат '{self.format}'. Допустимо: {VALID_FORMATS}.")
        if self.action not in VALID_ACTIONS:
            raise ValueError(f"Неизвестное действие '{self.action}'. Допустимо: {VALID_ACTIONS}.")
        if self.action == "bump" and self.interval_hours < 1:
            raise ValueError(
                f"Интервал bump должен быть >= 1 часа (получено {self.interval_hours})."
            )
        if self.action == "apply":
            _parse_time(self.apply_time)
        if self.apply_limit < 1:
            raise ValueError(f"Лимит откликов должен быть >= 1 (получено {self.apply_limit}).")


def _parse_time(hhmm: str) -> tuple[int, int]:
    """Разбирает 'HH:MM' → (hour, minute). Бросает ValueError при невалидном формате."""
    parts = hhmm.split(":")
    if len(parts) != 2:
        raise ValueError(f"Время ожидалось в формате HH:MM, получено '{hhmm}'.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Время ожидалось в формате HH:MM, получено '{hhmm}'.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Невалидное время суток: {hhmm}.")
    return hour, minute


def _program_arguments(action: str, apply_limit: int) -> list[str]:
    """Аргументы, которые планировщик передаёт scheduled_run.sh.

    bump — без лимита (дневной лимит и кулдаун 4ч держит throttle).
    apply — с --limit N (сверх дневного лимита, чтобы один прогон не
    выработал весь daily_apply_limit за раз).

    --headless ставится ПЕРВЫМ (до subcommand): это глобальный флаг корневого
    парсера cli.py, а не аргумент команды. `bump --headless` → argparse exit 2
    (unrecognized arguments); валидный порядок — `--headless bump ...`.
    Плановый прогон идёт без GUI, поэтому --headless захардкожен.
    """
    if action == "bump":
        return ["--headless", "bump"]
    return ["--headless", "apply", "--limit", str(apply_limit)]


def render_schedule(
    *,
    format: str,
    action: str,
    interval_hours: int = DEFAULT_BUMP_INTERVAL_HOURS,
    apply_time: str = DEFAULT_APPLY_TIME,
    apply_limit: int = DEFAULT_APPLY_LIMIT,
) -> str:
    """Генерирует текст конфига планировщика для копирования (чистая функция).

    Возвращает строку-ШАБЛОН (plist XML или crontab-запись) с плейсхолдерами
    __REPO_ROOT__ / __LOG_DIR__. Ничего не пишет на диск и не регистрирует
    агентов — только готовит текст для пользователя.
    """
    cfg = ScheduleConfig(
        format=format,
        action=action,
        interval_hours=interval_hours,
        apply_time=apply_time,
        apply_limit=apply_limit,
    )
    cfg.validate()

    if format == "plist":
        return _render_plist(cfg)
    return _render_crontab(cfg)


def _render_plist(cfg: ScheduleConfig) -> str:
    """Чистый launchd .plist (валидный XML), БЕЗ #-инструкций перед <?xml>.

    Инструкции живут отдельно (_instructions) и печатаются в stderr — тогда
    `hhru-bot schedule ... > x.plist` сохраняет в файл валидный plist, который
    launchd примет как есть (plutil -lint / plistlib не ругаются на '#').
    """
    wrapper = f"{PLACEHOLDER_REPO_ROOT}/scripts/scheduled_run.sh"
    args = [wrapper, *_program_arguments(cfg.action, cfg.apply_limit)]
    label = f"com.hhru.bot.{cfg.action}"
    log_out = f"{PLACEHOLDER_LOG_DIR}/scheduled.log"
    log_err = f"{PLACEHOLDER_LOG_DIR}/scheduled.log"

    start_block = _plist_start_block(cfg)

    # launchd даёт агенту УРЕЗАННЫЙ PATH и НЕ активирует venv проекта — голый
    # python3 резолвится в системный /usr/bin/python3 без playwright.
    # EnvironmentVariables>HHRU_PYTHON фиксирует интерпретатор (читается
    # scheduled_run.sh). PATH добавляем на всякий случай — chromium/playwright
    # могут искать системные утилиты.
    env_block = (
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        f"        <key>HHRU_PYTHON</key>\n"
        f"        <string>{PLACEHOLDER_PYTHON_BIN}</string>\n"
        "    </dict>"
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        f"    <string>{label}</string>",
        "    <key>ProgramArguments</key>",
        "    <array>",
        *[f"        <string>{a}</string>" for a in args],
        "    </array>",
        start_block,
        env_block,
        "    <key>StandardOutPath</key>",
        f"    <string>{log_out}</string>",
        "    <key>StandardErrorPath</key>",
        f"    <string>{log_err}</string>",
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def _plist_start_block(cfg: ScheduleConfig) -> str:
    """Блок запуска по расписанию: StartInterval для bump, StartCalendarInterval для apply."""
    if cfg.action == "bump":
        seconds = cfg.interval_hours * 3600
        return f"    <key>StartInterval</key>\n    <integer>{seconds}</integer>"
    hour, minute = _parse_time(cfg.apply_time)
    return (
        "    <key>StartCalendarInterval</key>\n"
        "    <dict>\n"
        "        <key>Hour</key>\n"
        f"        <integer>{hour}</integer>\n"
        "        <key>Minute</key>\n"
        f"        <integer>{minute}</integer>\n"
        "    </dict>"
    )


def _render_crontab(cfg: ScheduleConfig) -> str:
    """Чистая crontab-строка (расписание + команда), БЕЗ #-инструкций.

    Инструкции — в stderr через _instructions. Тогда `hhru-bot schedule
    --format crontab ...` можно вставить вывод напрямую в `crontab -e`
    (cron '#'-комментарии валидны, но единообразие с plist и тишина в stdout
    при редиректе важнее).
    """
    wrapper = f"{PLACEHOLDER_REPO_ROOT}/scripts/scheduled_run.sh"
    # Командная часть без пробелов внутри одного аргумента — собираем через join.
    cmd_args = " ".join(_program_arguments(cfg.action, cfg.apply_limit))
    # HHRU_PYTHON префиксом: cron НЕ активирует venv проекта, голый python3 в
    # cron-окружении не имеет playwright. scheduled_run.sh читает HHRU_PYTHON.
    command = f"HHRU_PYTHON={PLACEHOLDER_PYTHON_BIN} {wrapper} {cmd_args}"

    if cfg.action == "bump":
        # «0 */N * * *» срабатывает в начале каждого N-го часа от полуночи
        # (0:00, N:00, 2N:00, ...). Для N, не делящего 24, последний интервал
        # до следующей полуночи короче — это свойство cron, не баг. Для
        # дефолта N=4 (0,4,8,12,16,20) — ровно раз в 4 часа.
        schedule = f"0 */{cfg.interval_hours} * * *"
    else:
        hour, minute = _parse_time(cfg.apply_time)
        schedule = f"{minute} {hour} * * *"

    return f"{schedule} {command} >> {PLACEHOLDER_LOG_DIR}/scheduled.log 2>&1\n"


def _instructions(cfg: ScheduleConfig) -> str:
    """Человекочитаемые инструкции по установке (печатаются в stderr).

    Не часть генерируемого конфига: stdout содержит только валидный plist/
    crontab, чтобы редирект `> file` давал рабочий файл без правки.
    """
    if cfg.format == "plist":
        return (
            "launchd LaunchAgent (macOS) — ШАБЛОН. Скопируйте stdout в файл.\n"
            "Замените плейсхолдеры под свою машину:\n"
            f"  {PLACEHOLDER_REPO_ROOT} — путь к клону репозитория (напр. /Users/me/hhru)\n"
            f"  {PLACEHOLDER_LOG_DIR} — каталог логов (напр. {PLACEHOLDER_REPO_ROOT}/logs)\n"
            f"  {PLACEHOLDER_PYTHON_BIN} — абсолютный путь к python из venv проекта\n"
            f"    (напр. {PLACEHOLDER_REPO_ROOT}/.venv/bin/python; узнать: `which python`"
            " в активированном venv). launchd НЕ активирует venv — без этого\n"
            "    джоб упадёт на ModuleNotFoundError: playwright.\n"
            "Установка: cp файл в ~/Library/LaunchAgents/<label>.plist и\n"
            "  launchctl load ~/Library/LaunchAgents/<label>.plist\n"
            "Предохранители в коде (throttle.py) не дадут сработать раньше срока:\n"
            "  bump — кулдаун 4ч (can_bump_now), apply — дневной лимит (check_apply_limit).\n"
        )
    return (
        "crontab (Linux/macOS) — ШАБЛОН. Скопируйте строку из stdout.\n"
        "Замените плейсхолдеры под свою машину:\n"
        f"  {PLACEHOLDER_REPO_ROOT} — путь к клону репозитория\n"
        f"  {PLACEHOLDER_LOG_DIR} — каталог логов (напр. {PLACEHOLDER_REPO_ROOT}/logs)\n"
        f"  {PLACEHOLDER_PYTHON_BIN} — абсолютный путь к python из venv проекта\n"
        f"    (напр. {PLACEHOLDER_REPO_ROOT}/.venv/bin/python). cron НЕ активирует\n"
        "    venv — без этого джоб упадёт на ModuleNotFoundError: playwright.\n"
        "Установка: crontab -e и вставьте строку.\n"
        "Лог: обёртка scheduled_run.sh уже пишет в logs/scheduled.log.\n"
        "Предохранители в коде (throttle.py) не дадут сработать раньше срока.\n"
    )


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "schedule",
        help="Напечатать конфиг запуска по расписанию (launchd .plist или crontab)",
    )
    p.add_argument(
        "--format",
        choices=VALID_FORMATS,
        default="plist",
        help="Формат вывода: plist (launchd, по умолчанию) или crontab",
    )
    p.add_argument(
        "--action",
        choices=VALID_ACTIONS,
        default="bump",
        help="Какое действие планировать: bump (периодически) или apply (раз в день)",
    )
    p.add_argument(
        "--bump-interval-hours",
        type=int,
        default=DEFAULT_BUMP_INTERVAL_HOURS,
        help="Интервал запуска bump в часах (по умолчанию 4, равен кулдауну)",
    )
    p.add_argument(
        "--apply-time",
        default=DEFAULT_APPLY_TIME,
        help="Время ежедневного apply в формате HH:MM (по умолчанию 10:00)",
    )
    p.add_argument(
        "--apply-limit",
        type=int,
        default=DEFAULT_APPLY_LIMIT,
        help="Лимит откликов за один apply-прогон (по умолчанию 5)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    cfg = ScheduleConfig(
        format=args.format,
        action=args.action,
        interval_hours=args.bump_interval_hours,
        apply_time=args.apply_time,
        apply_limit=args.apply_limit,
    )
    try:
        cfg.validate()
        text = render_schedule(
            format=cfg.format,
            action=cfg.action,
            interval_hours=cfg.interval_hours,
            apply_time=cfg.apply_time,
            apply_limit=cfg.apply_limit,
        )
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    # Инструкции — в stderr, чтобы stdout содержал ТОЛЬКО конфиг: тогда
    # `hhru-bot schedule ... > x.plist` / `> crontab.txt` даёт рабочий файл
    # без ручной правки (для plist критично: '#' перед <?xml невалиден).
    print(_instructions(cfg), file=sys.stderr, end="")
    print(text)
