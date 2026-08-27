"""Тесты команды schedule (#18): генерация готовых .plist/crontab-конфигов.

Команда schedule — генератор конфигов, а не демон. Она только печатает текст
для копирования пользователем (launchd .plist или crontab). Здесь проверяем
чистую функцию render_schedule без запуска CLI — что вывод валиден и
содержит нужные вызовы bump/apply.

CLAUDE.md запрещает фоновые демоны в коде проекта; предохранители против
переоткликов/раннего бампа живут в throttle (check_apply_limit/can_bump_now),
а не здесь — schedule лишь «нажимает кнопку» по расписанию.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from hhru_bot.commands.schedule import render_schedule

pytestmark = pytest.mark.integration


def test_plist_bump_uses_start_interval():
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    # StartInterval считает в секундах — 4 часа = 14400
    assert "<key>StartInterval</key>" in out
    assert "<integer>14400</integer>" in out
    # Планировщик должен звать scheduled_run.sh (обёртку), а не сырой CLI
    assert "scheduled_run.sh" in out
    assert "bump" in out


def test_plist_apply_uses_start_calendar_interval():
    out = render_schedule(format="plist", action="apply", apply_time="10:00", apply_limit=5)
    assert "<key>StartCalendarInterval</key>" in out
    # 10:00 = Hour 10, Minute 0
    assert "<integer>10</integer>" in out
    assert "<integer>0</integer>" in out
    # apply должен передавать лимит откликов
    assert "apply" in out
    assert "--limit" in out
    assert "5" in out


def test_plist_parseable_by_plistlib():
    """Сгенерированный .plist — валидный XML property list (полный stdout, без преамбулы).

    Раньше этот тест вырезал #-преамбулу перед парсингом — и скрывал дефект, что
    полный stdout НЕ валиден как plist. Теперь валидируется немодифицированный
    вывод целиком (как его сохранит пользователь в ~/Library/LaunchAgents/).
    """
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    parsed = plistlib.loads(out.encode("utf-8"))
    assert isinstance(parsed, dict)
    # launchd .plist обязан содержать Label и ProgramArguments
    assert "Label" in parsed
    assert "ProgramArguments" in parsed
    assert isinstance(parsed["ProgramArguments"], list)
    assert len(parsed["ProgramArguments"]) >= 1
    # Логи направлены в файлы (StandardOutPath/StandardErrorPath)
    assert "StandardOutPath" in parsed
    assert "StandardErrorPath" in parsed


def test_plist_discards_scheduler_output():
    out = render_schedule(format="plist", action="apply", apply_time="10:00", apply_limit=3)
    parsed = _plist(out)
    # scheduled_run.sh already persists the output through tee; launchd must
    # not append the same stream to scheduled.log a second time.
    assert parsed["StandardOutPath"] == "/dev/null"
    assert parsed["StandardErrorPath"] == "/dev/null"


def test_crontab_format():
    out = render_schedule(format="crontab", action="bump", interval_hours=4)
    # crontab-запись содержит путь к обёртке
    assert "scheduled_run.sh" in out
    assert "bump" in out
    # 5 полей cron + команда (минимум 6 токенов в строке-задании)
    job_lines = [
        ln
        for ln in out.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#") and "scheduled_run.sh" in ln
    ]
    assert job_lines, "должна быть хотя бы одна crontab-строка с scheduled_run.sh"
    for line in job_lines:
        assert len(line.split()) >= 6
    assert ">>" not in out
    assert "scheduled.log" not in out


def test_crontab_apply_daily_at_time():
    out = render_schedule(format="crontab", action="apply", apply_time="09:30", apply_limit=7)
    assert "scheduled_run.sh" in out
    assert "apply" in out
    assert "--limit" in out
    assert "7" in out
    # 09:30 → cron «30 9 * * *»
    assert "30 9" in out


def test_invalid_interval_raises():
    with pytest.raises((ValueError, TypeError)):
        render_schedule(format="plist", action="bump", interval_hours=0)


def test_invalid_apply_time_raises():
    with pytest.raises((ValueError, TypeError)):
        render_schedule(format="plist", action="apply", apply_time="not-a-time")


def test_unknown_format_raises():
    with pytest.raises((ValueError, TypeError)):
        render_schedule(format="yaml", action="bump", interval_hours=4)


def test_unknown_action_raises():
    with pytest.raises((ValueError, TypeError)):
        render_schedule(format="plist", action="something", interval_hours=4)


def _plist(out: str):
    """Парсит полный stdout render_schedule как plist (без вырезания преамбулы)."""
    return plistlib.loads(out.encode("utf-8"))


# --- FIX: --headless должен идти ДО subcommand (глобальный флаг корневого парсера) ---


def test_program_arguments_headless_before_subcommand_bump():
    """--headless — глобальный флаг cli.py; после subcommand argparse его не примет.

    scheduled_run.sh пробрасывает argv дальше как есть, поэтому порядок в
    ProgramArguments критичен: `--headless bump ...`, НЕ `bump --headless ...`.
    """
    from hhru_bot.commands.schedule import _program_arguments

    assert _program_arguments("bump", 5) == ["--headless", "bump"]


def test_program_arguments_headless_before_subcommand_apply():
    from hhru_bot.commands.schedule import _program_arguments

    assert _program_arguments("apply", 5) == ["--headless", "apply", "--limit", "5"]


def test_program_arguments_preserve_account():
    from hhru_bot.commands.schedule import _program_arguments

    assert _program_arguments("bump", 5, "work") == ["--headless", "--account", "work", "bump"]


def test_program_arguments_preserve_resolved_explicit_paths():
    from hhru_bot.commands.schedule import _program_arguments

    assert _program_arguments("bump", 5, "work", "custom.yaml", "custom.db") == [
        "--headless",
        "--config",
        "custom.yaml",
        "--history",
        "custom.db",
        "bump",
    ]


def test_plist_programarguments_headless_before_action():
    """В сгенерированном .plist ProgramArguments[1] должен быть --headless, а не subcommand."""
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    parsed = _plist(out)
    argv = parsed["ProgramArguments"]
    # argv[0] = wrapper (scheduled_run.sh), argv[1] обязан быть --headless
    assert argv[1] == "--headless"
    assert argv[2] in {"bump", "apply"}


def test_cli_accepts_emitted_argv_bump():
    """End-to-end: эмитенный argv (через корневой парсер) НЕ должен падать с exit 2.

    `bump --headless` → argparse 'unrecognized arguments'. `--headless bump` —
    принимается (упадёт дальше по конфигу, но НЕ на разборе аргументов).
    Гарантирует, что planned-argv действительно запустится.
    """
    from hhru_bot.cli import build_parser

    parser = build_parser()
    for action in ("bump", "apply"):
        argv = ["--headless", action] + (["--limit", "5"] if action == "apply" else [])
        # parse_args бросает SystemExit(2) при 'unrecognized arguments'
        ns = parser.parse_args(argv)
        assert ns.headless is True
        assert ns.command == action


# --- FIX: полный stdout schedule — валидный plist (без shell-комментариев перед <?xml) ---


def test_full_plist_stdout_parseable_no_preamble():
    """Полный немодифицированный stdout должен быть валидным plist.

    Если _render_plist ставит #-инструкции перед <?xml, plutil/plistlib отвергают
    файл ('Unexpected character # at line 1'). Пользователь делает
    `hhru-bot schedule ... > x.plist` — он должен загрузиться в launchd как есть.
    """
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    # первая строка — xml-декларация, не shell-комментарий
    assert out.lstrip().startswith("<?xml"), "stdout должен начинаться с <?xml, без #-преамбулы"
    parsed = _plist(out)
    assert "Label" in parsed
    assert "ProgramArguments" in parsed


def test_full_plist_stdout_apply_parseable():
    out = render_schedule(format="plist", action="apply", apply_time="10:00", apply_limit=5)
    assert out.lstrip().startswith("<?xml")
    parsed = _plist(out)
    assert "StartCalendarInterval" in parsed


# --- FIX: launchd/cron имеют урезанный PATH и не активируют venv →
#     голый python3 падает на ModuleNotFoundError: playwright.
#     scheduled_run.sh читает HHRU_PYTHON; конфиги несут плейсхолдер __PYTHON_BIN__. ---


def test_plist_has_hhru_python_env_var():
    """launchd .plist должен задавать EnvironmentVariables>HHRU_PYTHON.

    launchd даёт агенту урезанный PATH и не активирует venv — без явного
    интерпретатора джоб упадёт на ModuleNotFoundError: playwright.
    """
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    parsed = _plist(out)
    env = parsed["EnvironmentVariables"]
    assert env["HHRU_PYTHON"] == "__PYTHON_BIN__"


def test_crontab_has_hhru_python_prefix():
    """crontab-строка должна иметь префикс HHRU_PYTHON=__PYTHON_BIN__.

    cron не активирует venv проекта; scheduled_run.sh читает HHRU_PYTHON.
    """
    out = render_schedule(format="crontab", action="bump", interval_hours=4)
    job = next(ln for ln in out.splitlines() if "scheduled_run.sh" in ln and not ln.startswith("#"))
    assert "HHRU_PYTHON=__PYTHON_BIN__" in job
    # префикс идёт ДО scheduled_run.sh (env-присваивание перед командой)
    assert job.index("HHRU_PYTHON=") < job.index("scheduled_run.sh")


@pytest.mark.parametrize(
    "path",
    [
        "scripts/crontab.example",
        "deploy/com.hhru.bot.apply.plist",
        "deploy/com.hhru.bot.bump.plist",
    ],
)
def test_shipped_scheduler_configs_have_one_persistent_log_writer(path):
    text = (Path(__file__).parents[1] / path).read_text()
    if path.endswith("crontab.example"):
        assert ">>" not in text
        assert "scheduled.log" not in text.splitlines()[-1]
    else:
        parsed = plistlib.loads(text.encode())
        assert parsed["StandardOutPath"] == "/dev/null"
        assert parsed["StandardErrorPath"] == "/dev/null"
