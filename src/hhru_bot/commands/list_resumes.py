"""Команда list-resumes (#57, live-дефолт — #320): список резюме (READ, #21).

Top-level команда ``hhru_bot list-resumes [--status] [--local]`` —
регистрируется автоматически через pkgutil.iter_modules (cli.py не трогается).

READ-команда. По умолчанию (#320) печатает канонический список резюме аккаунта
HH.ru: открывает /applicant/my_resumes под сохранённой сессией
(``copy_resume.list_resume_cards``) — реальный ``resume_id``, название, статус
(черновик/опубликовано), локальный alias из конфига (настройки-overlay).
Конфиг больше не реестр: его записи — только alias/настройки, отсутствие записи
не скрывает резюме (#319/#320). Флаг ``--local`` — офлайн-просмотр overlay из
``config.yaml`` без похода на hh.ru (hh.ru НЕ дёргается).

Один стабильный формат live-вывода: ``resume_id | alias | название | статус``;
``--status`` добавляет колонки «можно bump»/«последний bump» из ЛОКАЛЬНОЙ истории
(``throttle.can_bump_now``, ``history.last_action_at`` — keyed by resume_id,
поэтому работают для любых live-карточек, не только конфигурированных).
``--local`` печатает таблицу ``id | resume_id`` (+bump-колонки с ``--status``).

Никакого молчаливого fallback на локальный список при сбое live-чтения (#320):
невалидная сессия, отсутствующий cookie ``hhtoken``, форма входа при живом
cookie или indeterminate-состояние списка — каждое даёт явный ``[FAIL]``
и выход, иначе неполный локальный список выглядел бы достоверным.

``whoami._check_storage_state`` проверяет только формат файла сессии (валидный
JSON с cookies), НЕ факт актуальной авторизации на hh.ru — истёкшие cookies его
пройдут. Поэтому после открытия страницы дополнительно зовётся
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

Сироты overlay: записи ``config.resumes``, чей ``resume_id`` не встретился
среди live-карточек (резюме удалено на hh.ru) — ``[WARN]`` со списком, а не
молчаливое исчезновение настроек из виду.

Контракт вывода — docs/cli-spec.md §list-resumes. Статус читается из SSR
/applicant/my_resumes: ``not_finished`` → ``черновик``, остальные известные
значения → ``опубликовано``. ``alias`` — slug из конфига (настройки есть) или
«—» (remote-only, команды работают по resume_id — #319). Только текст/ASCII,
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
        help="Список резюме аккаунта с hh.ru (READ; --local — офлайн из конфига)",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Дополнительно: можно ли поднять (кулдаун) и дата последнего поднятия",
    )
    p.add_argument(
        "--local",
        action="store_true",
        help="Без похода на hh.ru: только записи config.yaml (overlay настроек)",
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
    """Колонка «статус»: ``черновик`` / ``опубликовано`` / «—»."""
    if status == "not_finished":
        return "черновик"
    if status in ("modified", "approved", "new", "finished"):
        return "опубликовано"
    if status:
        return status
    return "—"


def _live_rows(cards, alias_by_hash: dict[str, str], with_status: bool, throttle, history):
    """Строки live-таблицы: resume_id | alias | название | статус (| bump-колонки).

    ``alias_by_hash`` — {resume_id: slug} из config.resumes; «—» = remote-only
    (настроек нет, команды адресуются по resume_id — #319). Чистая функция
    по данным (тестируется без браузера/конфига); throttle/history — только
    для опциональных bump-колонок.
    """
    rows: list[list[str]] = []
    for card in cards:
        row = [
            card.resume_id,
            alias_by_hash.get(card.resume_id, "—"),
            card.title or "—",
            _format_status(card.status),
        ]
        if with_status:
            can_bump, wait_left = throttle.can_bump_now(card.resume_id)
            last_at = history.last_action_at(card.resume_id, "bump")
            row.append(_format_bump_cell(can_bump, wait_left))
            row.append(_format_last_bump_cell(last_at))
        rows.append(row)
    return rows


def run(args: argparse.Namespace) -> None:
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle
    from .whoami import _check_storage_state

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)

    if args.local:
        # Офлайн-просмотр overlay из конфига (hh.ru не дёргается). Явный режим:
        # по умолчанию список каноничен на hh.ru (#320).
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
        return

    # Дефолт (#320): канонический список резюме аккаунта с hh.ru.
    ok, detail, _ = _check_storage_state(Path(config.storage_state_file))
    if not ok:
        print(f"[FAIL] Сессия недействительна: {detail}. Выполните login.")
        return

    from ..browser import (
        RESUMES_FULL_LIST_URL,
        goto_hh,
        has_auth_cookie,
        has_login_form,
        launch_context,
    )
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
            # аккаунт. Не выдаём это за «резюме не найдено» (см. copy_resume.py);
            # локального fallback тоже нет (#320) — неполный список не должен
            # выглядеть достоверным.
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

    # Проверяем доступность SSR данных статусов
    ssr_unavailable_cards = [c for c in cards if c.ssr_unavailable]
    if ssr_unavailable_cards:
        print(
            "[WARN] Данные о статусе резюме недоступны (SSR не загрузился). "
            "Колонка «статус» может быть неточной для некоторых резюме."
        )

    alias_by_hash = {r.resume_id: r.id for r in config.resumes}
    header = ["resume_id", "alias", "название", "статус"]
    if args.status:
        header += ["можно bump", "последний bump"]
    print()
    print(_ascii_table(header, _live_rows(cards, alias_by_hash, args.status, throttle, history)))

    # Сироты overlay: настройка есть, резюме на hh.ru нет (удалено?) — не молча.
    live_ids = {c.resume_id for c in cards}
    orphans = [(r.id, r.resume_id) for r in config.resumes if r.resume_id not in live_ids]
    if orphans:
        print()
        print("[WARN] Записи конфига без резюме на hh.ru (настройки не применяются):")
        for slug, resume_hash in orphans:
            print(f"  - {slug} = {resume_hash}")

    not_configured = [c for c in cards if c.resume_id not in alias_by_hash]
    if not_configured:
        from .copy_resume import format_config_snippet

        print()
        print(
            "[INFO] Резюме без настроек в конфиге — команды адресуются по resume_id "
            "(#319); overlay-настройки (search/ai_profile/...) опциональны:"
        )
        for card in not_configured:
            print(format_config_snippet(card.resume_id))
