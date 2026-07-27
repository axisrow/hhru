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
    """
    if action == "bump":
        return ["bump", "--headless"]
    return ["apply", "--headless", "--limit", str(apply_limit)]


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
    wrapper = f"{PLACEHOLDER_REPO_ROOT}/scripts/scheduled_run.sh"
    args = [wrapper, *_program_arguments(cfg.action, cfg.apply_limit)]
    label = f"com.hhru.bot.{cfg.action}"
    log_out = f"{PLACEHOLDER_LOG_DIR}/scheduled.log"
    log_err = f"{PLACEHOLDER_LOG_DIR}/scheduled.log"

    start_block = _plist_start_block(cfg)

    # XML собираем вручную, чтобы сохранить читаемый шаблонный вид с комментариями
    # и плейсхолдерами (plistlib escapes всё и теряет комментарии-инструкции).
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
        "    <key>StandardOutPath</key>",
        f"    <string>{log_out}</string>",
        "    <key>StandardErrorPath</key>",
        f"    <string>{log_err}</string>",
        "</dict>",
        "</plist>",
    ]
    header = (
        "# launchd LaunchAgent (macOS) — ШАБЛОН.\n"
        "# Замените плейсхолдеры под свою машину:\n"
        f"#   {PLACEHOLDER_REPO_ROOT} — путь к клону репозитория (напр. /Users/me/hhru)\n"
        f"#   {PLACEHOLDER_LOG_DIR} — каталог логов (напр. {PLACEHOLDER_REPO_ROOT}/logs)\n"
        "# Установка: скопируйте в ~/Library/LaunchAgents/<label>.plist и\n"
        "#   launchctl load ~/Library/LaunchAgents/<label>.plist\n"
        "# Предохранители в коде (throttle.py) не дадут сработать раньше срока:\n"
        "#   bump — кулдаун 4ч (can_bump_now), apply — дневной лимит (check_apply_limit).\n"
        "#\n"
    )
    return header + "\n".join(lines) + "\n"


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
    wrapper = f"{PLACEHOLDER_REPO_ROOT}/scripts/scheduled_run.sh"
    # Командная часть без пробелов внутри одного аргумента — собираем через join.
    cmd_args = " ".join(_program_arguments(cfg.action, cfg.apply_limit))
    command = f"{wrapper} {cmd_args}"

    if cfg.action == "bump":
        # каждые N часов: запуск в :00 каждого N-го часа дня.
        # «0 */N * * *» срабатывает раз в N часов в начале часа.
        schedule = f"0 */{cfg.interval_hours} * * *"
    else:
        hour, minute = _parse_time(cfg.apply_time)
        schedule = f"{minute} {hour} * * *"

    header = (
        "# crontab (Linux/macOS) — ШАБЛОН.\n"
        "# Замените плейсхолдеры под свою машину:\n"
        f"#   {PLACEHOLDER_REPO_ROOT} — путь к клону репозитория\n"
        f"#   {PLACEHOLDER_LOG_DIR} — каталог логов (напр. {PLACEHOLDER_REPO_ROOT}/logs)\n"
        "# Установка: crontab -e и вставьте строки ниже.\n"
        "# Лог: обёртка scheduled_run.sh уже пишет в logs/scheduled.log.\n"
        "# Предохранители в коде (throttle.py) не дадут сработать раньше срока.\n"
        "#\n"
    )
    return f"{header}{schedule} {command} >> {PLACEHOLDER_LOG_DIR}/scheduled.log 2>&1\n"


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
    try:
        text = render_schedule(
            format=args.format,
            action=args.action,
            interval_hours=args.bump_interval_hours,
            apply_time=args.apply_time,
            apply_limit=args.apply_limit,
        )
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    print(text)
