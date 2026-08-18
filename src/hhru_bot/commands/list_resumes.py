"""Команда list-resumes (#57, --remote — #135): список резюме (READ, #21).

Top-level команда ``hhru_bot list-resumes [--status] [--remote]`` —
регистрируется автоматически через pkgutil.iter_modules (cli.py не трогается).

READ-команда: по умолчанию читает ``config.resumes`` локально из config.yaml
и печатает ASCII-таблицу (``report._ascii_table``). hh.ru НЕ дёргается.

Флаг ``--status`` дополнительно показывает статус поднятия резюме, читая ЛОКАЛЬНУЮ
историю (без обращения к аккаунту):
  - «можно bump» — ``throttle.can_bump_now()`` (кулдаун 4ч сверх дневного лимита);
  - «последний bump» — ``history.last_action_at(resume_id, 'bump')`` (дата
    последнего успешного поднятия из actions, или «—» если не поднимали).

Флаг ``--remote`` (#135, #315) — read-only поход на hh.ru: открывает
/applicant/my_resumes под сохранённой сессией (``copy_resume.list_resume_cards``)
и печатает реальные резюме аккаунта (хэш, название, статус, подключено ли в конфиге).
Черновики (status=not_finished) показываются отдельно от опубликованных.
Разблокирует ``copy-resume`` (#116), в конфиге которого resume_url — плейсхолдеры.
Только ``goto`` + чтение DOM/SSR — ничего не кликается и не отправляется, поэтому
подтверждение WRITE (``--force``) не требуется. Совместим с ``--status``
(тот читает локальную историю — конфликта источников данных нет).

``whoami._check_storage_state`` проверяет только формат файла сессии (валидный
JSON с cookies), НЕ факт актуальной авторизации на hh.ru — истёкшие cookies его
пройдут. Поэтому после открытия страницы ``--remote`` дополнительно зовёт
``browser.has_auth_cookie(page)`` (наличие cookie ``hhtoken``); без этой
проверки истёкшая сессия и вправду пустой аккаунт неотличимы (оба дают
0 карточек), и команда напечатала бы обманчивое «резюме не найдено» вместо
«сессия недействительна». Именно cookie hhtoken, не URL-проверка — та
ненадёжна для этой цели (см.
auth.py:59-61: hh.ru может оставить путь входа в редиректе даже при
успешном входе).

Cookie в jar не гарантирует, что сервер принял её на текущей странице
(issue #147: истёкший/отозванный ``hhtoken`` может остаться в jar без
явного ``Set-Cookie`` на очистку) — ``browser.has_login_form(page)``
дополнительно проверяет подтверждённый DOM-маркер серверной формы входа
(``[data-qa='account-login-form']``); её наличие при present-cookie тоже
трактуется как «сессия недействительна», а не как «резюме не найдено».

Заголовок резюме (``RESUME_LIST_CARD_TITLE``) — селектор НЕ подтверждён живым
дампом (см. selector_groups/resume_list.py). Если он не совпал ни для одной
карточки, при этом карточки есть, — печатается предупреждение о ненадёжности
колонки «название», а не молчаливый прочерк, выдаваемый за подтверждённые данные.

Контракт вывода — docs/cli-spec.md §list-resumes: базовые колонки
``id | resume_id | можно bump | последний bump`` (последние две — только с
``--status``); ``--remote`` печатает отдельную таблицу
``resume_id | название | статус | в конфиге``. Статус читается из SSR
/applicant/my_resumes: ``not_finished`` → ``черновик``, остальные известные
значения → ``опубликовано``. ``id`` — slug из конфига; ``resume_id`` —
числовой хвост ``resume_url`` (``ResumeConfig.resume_id``). Только текст/ASCII,
без эмодзи.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from ..report import _ascii_table


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "list-resumes",
        help="Список резюме (READ; --status — из истории, --remote — с hh.ru)",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Дополнительно: можно ли поднять (кулдаун) и дата последнего поднятия",
    )
    p.add_argument(
        "--remote",
        action="store_true",
        help="Показать реальные резюме аккаунта с hh.ru (read-only, требует сессии)",
    )
    p.set_defaults(func=run)


def _format_bump_cell(can_bump: bool, wait_left) -> str:
    """Колонка «можно bump»: «да» / «нет (Xч Yм)».
    wait_left — timedelta | None: None при can_bump=True (кулдаун прошёл/не было bump).
    """
    if can_bump:
        return "да"
    # can_bump=False → wait_left гарантированно не None (см. throttle.can_bump_now).
    total_seconds = int(wait_left.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"нет ({hours}ч {minutes}м)"
    return f"нет ({minutes}м)"


def _format_last_bump_cell(last_at: datetime | None) -> str:
    """Колонка «последний bump»: «YYYY-MM-DD HH:MM» или «—» (нет успешных bump)."""
    if last_at is None:
        return "—"
    return last_at.strftime("%Y-%m-%d %H:%M")


def _format_status(status: str | None) -> str:
    """Колонка «статус»: ``черновик`` / ``опубликовано`` / ``—``."""
    if status == "not_finished":
        return "черновик"
    if status in ("modified", "approved", "new", "finished"):
        return "опубликовано"
    if status:
        return status
    return "—"


def _remote_rows(cards, configured_ids: set[str]) -> list[list[str]]:
    """Строки таблицы --remote: resume_id | название | статус | в конфиге.

    «в конфиге» — id резюме из config.resumes, если resume_id карточки совпал
    с ResumeConfig.resume_id какого-то элемента (передаётся отдельно, т.к.
    сама функция чистая — тестируется без браузера/конфига)."""
    rows: list[list[str]] = []
    for card in cards:
        in_config = "да" if card.resume_id in configured_ids else "—"
        rows.append([card.resume_id, card.title or "—", _format_status(card.status), in_config])
    return rows


def run(args: argparse.Namespace) -> None:
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle
    from .whoami import _check_storage_state

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)

    if args.status:
        header = ["id", "resume_id", "можно bump", "последний bump"]
    else:
        header = ["id", "resume_id"]

    rows: list[list[str]] = []
    for resume in config.resumes:
        row = [resume.id, resume.resume_id]
        if args.status:
            can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
            last_at = history.last_action_at(resume.resume_id, "bump")
            row.append(_format_bump_cell(can_bump, wait_left))
            row.append(_format_last_bump_cell(last_at))
        rows.append(row)

    print(_ascii_table(header, rows))

    if not args.remote:
        return

    ok, detail, _ = _check_storage_state(Path(config.storage_state_file))
    if not ok:
        print(f"[FAIL] Сессия недействительна: {detail}. Выполните login.")
        return

    from ..browser import RESUMES_FULL_LIST_URL, goto_hh, has_auth_cookie, has_login_form, launch_context
    from ..copy_resume import ResumeListIndeterminate, list_resume_cards

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        # _check_storage_state выше проверил только формат файла — не факт актуальной
        # авторизации на hh.ru. Без этой проверки истёкшая сессия неотличима от
        # пустого аккаунта резюме (обе дают 0 карточек ниже). Проверяем через
        # cookie hhtoken (Codex review), НЕ через URL — hh.ru может оставить
        # путь входа в редиректе даже при
        # успешном входе, что дало бы ложный [FAIL] для валидной сессии.
        if not has_auth_cookie(page):
            print("[FAIL] Сессия недействительна (cookie hhtoken не найден). Выполните login.")
            return
        # #147 (Codex adversarial review, PR #152): has_login_form читает DOM
        # текущей страницы — на свежей странице context.new_page() (ещё без
        # навигации) это всегда 0 совпадений, что сделало бы проверку фиктивной
        # независимо от реального состояния сессии. Поэтому переходим на
        # RESUMES_FULL_LIST_URL здесь, ДО проверки, и list_resume_cards ниже вызывается
        # с navigate=False, чтобы не переходить туда же повторно.
        goto_hh(page, RESUMES_FULL_LIST_URL)
        # устаревший/отозванный hhtoken может остаться в jar без явного
        # Set-Cookie на очистку — cookie сама по себе не подтверждает, что
        # сервер принял сессию на текущей странице. Форма входа — подтверждённый
        # позитивный DOM-маркер отказа сервера (см. browser.has_login_form).
        if has_login_form(page):
            print(
                "[FAIL] Сессия недействительна (страница содержит форму входа "
                "при наличии hhtoken). Выполните login."
            )
            return
        try:
            cards = list_resume_cards(page, navigate=False)
        except ResumeListIndeterminate as e:
            # Timeout/интерстишл/дрейф селектора — не подтверждённо пустой
            # аккаунт. Не выдаём это за «резюме не найдено» (см. copy_resume.py).
            print(f"[FAIL] {e}")
            return

    if not cards:
        print("[INFO] На hh.ru не найдено ни одного резюме.")
        return

    if not any(c.title for c in cards):
        print(
            "[INFO] Название резюме не удалось прочитать (селектор заголовка "
            "не подтверждён) — колонка «название» может быть неточной."
        )

    configured_ids = {r.resume_id for r in config.resumes}
    print()
    print(_ascii_table(["resume_id", "название", "статус", "в конфиге"], _remote_rows(cards, configured_ids)))

    not_configured = [c for c in cards if c.resume_id not in configured_ids]
    if not_configured:
        from .copy_resume import format_config_snippet

        print()
        print("[INFO] Резюме не в конфиге — добавьте вручную:")
        for card in not_configured:
            print(format_config_snippet(card.resume_id))
