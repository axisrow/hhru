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

import pytest

from hhru_bot.commands.schedule import render_schedule


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
    """Сгенерированный .plist — валидный XML property list."""
    out = render_schedule(format="plist", action="bump", interval_hours=4)
    # plistlib парсит XML; до и после plist-блока могут быть комментарии —
    # вырежем чисто XML-документ (от <?xml ... до закрывающего </plist>).
    start = out.index("<?xml")
    end = out.index("</plist>") + len("</plist>")
    parsed = plistlib.loads(out[start:end].encode("utf-8"))
    assert isinstance(parsed, dict)
    # launchd .plist обязан содержать Label и ProgramArguments
    assert "Label" in parsed
    assert "ProgramArguments" in parsed
    assert isinstance(parsed["ProgramArguments"], list)
    assert len(parsed["ProgramArguments"]) >= 1
    # Логи направлены в файлы (StandardOutPath/StandardErrorPath)
    assert "StandardOutPath" in parsed
    assert "StandardErrorPath" in parsed


def test_plist_logs_to_scheduled_log():
    out = render_schedule(format="plist", action="apply", apply_time="10:00", apply_limit=3)
    assert "scheduled.log" in out


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
