"""Команда whoami: действительность сессии + сводка аккаунта (READ, #56, #21).

Показывает, что сохранённая сессия (`storage_state_file`) действительна, и сводку
по аккаунту. Счётчики берутся из ЛОКАЛЬНОЙ истории (actions/responses) — whoami
НЕ ходит на hh.ru за динамикой: просмотр/приглашение/ответ наполняет таблица
``responses`` (#12) из отдельной команды ``responses``. Так whoami — мгновенный
read-only дашборд без браузера и риска анти-фрода.

Вывод — ASCII-таблица через ``report._ascii_table`` (НЕ дублируется) + строка
``[INFO]`` / ``[FAIL] ...``. Только текст — НИКАКИХ эмодзи
(правило проекта: CLI-вывод чистый). Эмодзи-строка референса s3rgeym (иконка +
счётчики в скобках) здесь намеренно заменена таблицей (контракт docs/cli-spec.md
§whoami).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ONLINE_CHECK_URL = "https://hh.ru/applicant/resumes"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "whoami",
        help="Проверить сессию и показать сводку аккаунта (из локальной истории)",
    )
    p.add_argument("--resume", help="Slug из конфига или resume_id HH.ru (по умолчанию — все)")
    p.add_argument(
        "--online",
        action="store_true",
        help="Проверить сессию на hh.ru (открывает браузер)",
    )
    p.set_defaults(func=run)


def _check_storage_state(storage_state_file: Path) -> tuple[bool, str, dict | None]:
    """Проверяет формат storage_state и возвращает разобранный JSON."""
    if not storage_state_file.exists():
        return False, f"файл сессии не найден: {storage_state_file}", None
    try:
        raw = json.loads(storage_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"файл сессии нечитаем: {e}", None
    if not isinstance(raw, dict) or not isinstance(raw.get("cookies"), list):
        return False, "файл сессии некорректен (нет cookies)", None
    return True, "", raw


def _check_session(storage_state_file: Path) -> tuple[bool, str]:
    """Действительна ли сохранённая сессия — без браузера.

    Проверяем существование файла и наличие cookie ``hhtoken`` в валидном
    Playwright storage_state. НЕ открываем браузер и НЕ тыкаем hh.ru: whoami
    должен быть мгновенным и read-only. Истёкшую куку без браузера определить
    нельзя, поэтому это локальная проверка сохранённого auth-маркера.

    Возвращает (ok, detail): detail — человекочитаемая причина для строки [FAIL].
    """
    ok, detail, raw = _check_storage_state(storage_state_file)
    if not ok:
        return False, detail
    assert raw is not None
    if not any(c.get("name") == "hhtoken" for c in raw["cookies"] if isinstance(c, dict)):
        return False, "в файле сессии отсутствует cookie hhtoken"
    return True, ""


def _resumes_for(config, args: argparse.Namespace):
    """Резюме под сводку: --resume → одно (slug или resume_id HH.ru, #319), иначе все."""
    if args.resume is None:
        return config.resumes
    from ..config import ConfigError
    from ._common import resolve_resume

    try:
        return [resolve_resume(config, args.resume)]
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)


def run(args: argparse.Namespace) -> None:
    from ..config import load_config_or_exit
    from ..history import History
    from ..report import _ascii_table

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = _resumes_for(config, args)

    # 1) Сессия.
    ok, detail = _check_session(Path(config.storage_state_file))
    if not ok:
        print(f"[FAIL] Сессия недействительна: {detail}")
    elif getattr(args, "online", False):
        from playwright.sync_api import Error as PlaywrightError

        from ..browser import goto_hh, has_auth_cookie, has_login_form, launch_context

        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            try:
                goto_hh(page, ONLINE_CHECK_URL)
            except PlaywrightError as e:
                # Недостижимость страницы (сеть/DDoS-Guard/таймаут после retries
                # в goto_hh) — неподтверждённое состояние, а не доказанный отказ
                # сервера. Не выдаём это ни за валидную, ни молча за невалидную
                # сессию (принцип browser.PageStateIndeterminate, CLAUDE.md #5).
                print(f"[FAIL] Не удалось проверить сессию на hh.ru: {e}")
            else:
                if not has_auth_cookie(page) or has_login_form(page):
                    print("[FAIL] Сессия недействительна на hh.ru. Выполните login.")
                else:
                    print("[INFO] Сессия действительна (проверено на hh.ru)")
    else:
        print(
            "[INFO] Локальный auth-маркер найден; для проверки сессии на hh.ru используйте --online"
        )

    # 2) Счётчики из локальной истории (НЕ с hh.ru).
    # Откликов сегодня — сумма по выбранным резюме, под resume.resume_id
    # (числовой id hh.ru — тот же ключ, что apply/bump пишут в БД).
    applied_today = sum(history.count_today(r.resume_id, "apply") for r in resumes)
    apply_limit = config.throttle.daily_apply_limit

    # Responses — account-scope (#12): страница /applicant/negotiations общая,
    # достоверной принадлежности ответа конкретному резюме нет. Поэтому счётчики
    # ответов/приглашений — по аккаунту, --resume их НЕ сужает (как responses.py).
    now = datetime.now()
    new_responses = len(history.new_responses_since(now - timedelta(hours=24)))
    all_responses = history.new_responses_since(datetime.min)
    invitations = sum(1 for r in all_responses if r.get("status") == "invitation")
    pending_tests = len(history.test_assignments_since(datetime.min))

    resume_label = ", ".join(r.id for r in resumes) or "(нет резюме в конфиге)"

    rows = [
        ["Резюме", resume_label],
        ["Откликов сегодня", f"{applied_today} / {apply_limit}"],
        ["Новых ответов 24ч", str(new_responses)],
        ["Приглашений", str(invitations)],
        ["Тестов ожидает", str(pending_tests)],
    ]

    print(_ascii_table(["Поле", "Значение"], rows))
