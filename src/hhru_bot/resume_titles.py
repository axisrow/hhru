"""Уникальность должностей в рамках аккаунта hh.ru (#911).

Пользователь установил вручную (2026-09-01, живая проверка в Chrome): все
должности в одном аккаунте уникальны, и если целевая должность уже существует
1 в 1 — сохранение молча не проходит, сколько ни кликай. Отказ hh.ru при этом
невидим: экран не показывает ошибки, визард не переходит. Поэтому каждый путь
записи должности (create-resume, copy-resume, resume-position) обязан
отклонять дубликат ДО первого мутирующего клика — после клика отличить
«молча не сохранилось» от «сохранилось» уже нельзя без отдельной проверки.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import RESUMES_FULL_LIST_URL, goto_hh
from .external_forms.detect import normalize
from .resume_ids import card_resume_id
from .selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_TITLE,
)
from .selector_groups.resume_page import RESUME_CREATE_BUTTON

# Тот же якорь гидрации, что у create_resume (#304): список может легитимно
# быть пустым, поэтому якорится кнопка создания, а не карточки. Щедрый, но
# конечный таймаут — commit не значит «отрисовано» (CLAUDE.md, паттерн
# «commit не значит отрисовано»).
_LIST_ANCHOR_TIMEOUT_MS = 15_000


@dataclass(frozen=True)
class AccountTitle:
    """Название одного резюме аккаунта, привязанное к его resume_id."""

    resume_id: str
    title: str


def read_account_titles(page: Page) -> tuple[list[AccountTitle], str]:
    """Прочитать ``(resume_id, название)`` всех резюме с экрана списка.

    Контракт: вызывающий код уже открыл и гидратировал экран списка
    (create-resume — своей кнопкой-якорем до этого вызова; остальные входы —
    через :func:`account_duplicate_reason`). Пустой аккаунт — ``([], "")``:
    у него кнопка создания есть, карточек нет. Ни карточек, ни кнопки —
    неотрисованный экран, а не пустой аккаунт: проверка дубля невозможна,
    поэтому отказ, а не молчаливое разрешение (fail-closed).

    Карточка без читаемого resume_id или названия — дрейф разметки: частичный
    список не может доказать отсутствие дубля, поэтому тоже отказ. Тот же
    инвариант, что у create_resume (циклы Codex-review 2/3) и у
    ``copy_resume.list_resume_cards`` (PR #322): неполный список — не список.

    Функция тотальна: Playwright-ошибки чтения (детач карточки ререндером
    между ``count()`` и чтением, отказ рендерера) конвертируются в ту же
    причину отказа — команды получают обычный fail-closed ``[FAIL]``, а не
    трейсбек из префлайта (ревью PR #912: в create/copy-вызовах общего
    обработчика нет, BaseException-ветка copy-resume ретраит исключение).
    """
    cards = page.locator(RESUME_LIST_CARD)
    try:
        if cards.count() == 0:
            if page.locator(RESUME_CREATE_BUTTON).count() == 0:
                return [], "список резюме не отрисовался: проверка дубля должности невозможна"
            return [], ""
        entries: list[AccountTitle] = []
        for card in cards.all():
            # Чтение resume_id через общий ридер (#891): снапшот .all() убирает
            # ожидание на ПОИСК ссылок («счёт строго до чтения» — без retry-гонки
            # между count() и перечислением). Само чтение атрибута авто-ждёт и
            # при детаче между .all() и get_attribute кидает TimeoutError — это
            # покрывает внешний except PlaywrightError ниже, не снимать его как
            # якобы избыточный.
            resume_id = card_resume_id(card)
            if not resume_id:
                return [], (
                    "карточка резюме без resume_id (ссылка-хэш не найдена — дрейф разметки); "
                    "список не подтверждён, запись запрещена"
                )
            title_locator = card.locator(RESUME_LIST_CARD_TITLE)
            title = ""
            if title_locator.count() == 1:
                title = (title_locator.first.inner_text() or "").strip()
            if not title:
                return [], (
                    "не удалось прочитать заголовки всех существующих резюме; запись запрещена"
                )
            entries.append(AccountTitle(resume_id=resume_id, title=title))
        return entries, ""
    except PlaywrightError as exc:
        return [], f"не удалось прочитать список резюме: {exc}"


def duplicate_title_reason(
    entries: list[AccountTitle], title: str, *, exclude_resume_id: str = ""
) -> str:
    """``""`` — писать можно; иначе причина отказа (чистая, тестируемая без браузера).

    ``exclude_resume_id`` — резюме, чья карточка игнорируется при сравнении:
    сохранение должности, которую это резюме уже носит, — не дубль (менять
    своё собственное название на него же можно). create/copy ничего не
    исключают: их цель ещё не существует в списке.
    """
    if not entries:
        return ""
    others = {entry.title for entry in entries if entry.resume_id != exclude_resume_id}
    if normalize(title) in {normalize(existing) for existing in others}:
        return (
            f"резюме с должностью «{title}» уже существует; должности в аккаунте "
            "уникальны, запись запрещена"
        )
    return ""


def account_duplicate_reason(page: Page, title: str, *, exclude_resume_id: str = "") -> str:
    """Открыть список, дождаться гидрации и проверить дубликат должности.

    Один вызов для команд copy/resume-position: навигация + якорь
    «кнопка создания ИЛИ карточка» (на исчерпанном лимите резюме hh.ru кнопку
    не рендерит вовсе — карточки при этом читаемы) + чтение + чистая проверка.
    Тотальна, как и :func:`read_account_titles`: отказ навигации — тоже
    «проверка невозможна» с причиной, а не исключение в команду.
    """
    try:
        goto_hh(page, RESUMES_FULL_LIST_URL)
    except PlaywrightError as exc:
        return f"не удалось открыть список резюме: {exc}"
    anchor = page.locator(RESUME_CREATE_BUTTON).or_(page.locator(RESUME_LIST_CARD)).first
    try:
        anchor.wait_for(state="visible", timeout=_LIST_ANCHOR_TIMEOUT_MS)
    except PlaywrightError as exc:
        return f"список резюме не отрисовался: {exc}"
    entries, reason = read_account_titles(page)
    if reason:
        return reason
    return duplicate_title_reason(entries, title, exclude_resume_id=exclude_resume_id)
