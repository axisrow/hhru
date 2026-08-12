"""Копирование резюме на hh.ru («Дублировать» в терминах hh.ru, #116).

Браузерный шаг команды copy-resume. Клик по пункту «Дублировать» отправляет
POST /applicant/resumes/clone?resume=<hash>, после чего фронт hh.ru переходит
на страницу нового резюме. Кнопка НЕ рендерится, когда достигнут лимит резюме
hh.ru (~20) — это единственный видимый признак лимита, поэтому её отсутствие
трактуем как отказ, а не как «селектор устарел».

Fail-closed (#33): карточка резюме привязывается к resume_id через
resume-card-link-<hash> (identity-bound); новый resume_id обязан отличаться от
исходного и определяться однозначно, иначе — неуспех без угадывания.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh
from .config import ResumeConfig
from .selector_groups.resume_list import (
    RESUME_DUPLICATE_INLINE,
    RESUME_DUPLICATE_MENU_ITEM,
    RESUME_LIST_ACTION_MORE,
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
    RESUME_LIST_CARD_TITLE,
)

logger = logging.getLogger("hhru_bot.copy_resume")

RESUMES_LIST_URL = f"{HH_BASE_URL}/applicant/resumes"
COPY_TIMEOUT_MS = 30_000

_RESUME_HASH_RE = re.compile(r"/resume/([0-9a-f]{32,40})")
_CARD_LINK_PREFIX = "resume-card-link-"


@dataclass
class CopyResumeResult:
    resume_id: str
    success: bool
    new_resume_id: str = ""
    reason: str = ""


@dataclass
class ResumeCard:
    resume_id: str
    title: str
    url: str


class ResumeListIndeterminate(Exception):
    """Не удалось подтвердить состояние /applicant/resumes (#135, Codex review).

    Ни одна карточка не появилась за COPY_TIMEOUT_MS после навигации. Без
    подтверждённого маркера «список действительно пуст» это неотличимо от
    честно пустого аккаунта, анти-бот/интерстишл-страницы, деградировавшей
    загрузки или дрейфа RESUME_LIST_CARD. list_resume_cards поэтому не молчит
    и не выдаёт пустой список за подтверждённый факт — поднимает это
    исключение, чтобы вызывающий код (list_resumes.py --remote) сообщил
    «состояние не подтверждено», а не соврал «резюме не найдено»."""


def _card_hashes(page: Page) -> set[str]:
    """Хэши всех резюме в списке /applicant/resumes (для diff до/после)."""
    hashes: set[str] = set()
    for link in page.locator(f"[data-qa^='{_CARD_LINK_PREFIX}']").all():
        qa = link.get_attribute("data-qa") or ""
        if qa.startswith(_CARD_LINK_PREFIX):
            hashes.add(qa[len(_CARD_LINK_PREFIX) :])
    return hashes


def list_resume_cards(page: Page) -> list[ResumeCard]:
    """Список резюме аккаунта с /applicant/resumes: хэш + название + URL (#135).

    READ-only: только goto + чтение DOM, ничего не кликается и не отправляется.
    Заголовок читается ПОД каждой карточкой (RESUME_LIST_CARD), не под page —
    тот же принцип, что и для кнопки «Дублировать» (см. copy_resume_on_hh:
    page.locator(...).first взял бы первую в DOM-порядке при нескольких резюме).
    RESUME_LIST_CARD_TITLE не подтверждён живым дампом — его отсутствие даёт
    title="", а не исключение.

    Playwright ``Locator.all()`` резолвит элементы, присутствующие ПРЯМО СЕЙЧАС,
    и не ждёт их появления — в отличие от copy_resume_on_hh, которая после
    goto_hh делает явный wait_for на карточке. Без такого ожидания здесь
    медленный рендер /applicant/resumes (или анти-бот/интерстишл-страница)
    даёт 0 карточек «прямо сейчас», что неотличимо от честно пустого
    аккаунта — команда солгала бы «резюме не найдено». Поэтому при первом
    count()==0 ждём появления хотя бы одной карточки коротким wait_for.

    Если карточка так и не появилась за это время — состояние страницы НЕ
    подтверждено (см. ResumeListIndeterminate): нет надёжного маркера «список
    действительно пуст», отличить честно пустой аккаунт от timeout/интерстишла/
    дрейфа селектора здесь нельзя, поэтому функция не молчит и не выдаёт
    пустой список за факт — поднимает исключение.
    """
    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    goto_hh(page, RESUMES_LIST_URL)

    cards_locator = page.locator(RESUME_LIST_CARD)
    if cards_locator.count() == 0:
        try:
            cards_locator.first.wait_for(timeout=COPY_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise ResumeListIndeterminate(
                "карточки резюме не появились за отведённое время — состояние "
                "/applicant/resumes не подтверждено (timeout, анти-бот/"
                "интерстишл-страница или дрейф селектора RESUME_LIST_CARD)"
            ) from None

    cards: list[ResumeCard] = []
    for card in cards_locator.all():
        resume_id = ""
        for link in card.locator(f"[data-qa^='{_CARD_LINK_PREFIX}']").all():
            qa = link.get_attribute("data-qa") or ""
            if qa.startswith(_CARD_LINK_PREFIX):
                resume_id = qa[len(_CARD_LINK_PREFIX) :]
                break
        if not resume_id:
            continue

        title = ""
        title_locator = card.locator(RESUME_LIST_CARD_TITLE)
        if title_locator.count() == 1:
            title = (title_locator.first.inner_text() or "").strip()

        url = f"{HH_BASE_URL}/resume/{resume_id}"
        cards.append(ResumeCard(resume_id=resume_id, title=title, url=url))
    return cards


def _wait_single_match_count(locator, *, timeout_ms: int) -> int:
    """Идиома ``count → wait_for → count`` для строгого (strict-mode) Playwright.

    Возвращает итоговое число совпадений, либо ``-1`` (элемент не появился за
    ``timeout_ms`` — PlaywrightTimeoutError). Контракт callsite: ``1`` — ок,
    ``0``-после-wait или ``-1`` — не найдено, ``>1`` — неоднозначно (fail-closed).

    Зачем ``count()`` ДО ``wait_for``: Playwright-локаторы строгие — ``wait_for()``
    на локаторе с >1 совпадением кидает ``playwright.sync_api.Error`` ("strict mode
    violation"), НЕ ``TimeoutError``. Проверка ``count() != 1`` первой ловит
    неоднозначность предсказуемо (fail-closed), а не улетает необработанным
    исключением мимо ``cli.main`` (там ловится только ``KeyboardInterrupt``).

    #142: идиома повторялась дважды (карточка резюме + кнопка «Дублировать»),
    вынесена сюда, чтобы правки strict-mode-логики были в одном месте.
    """
    match_count = locator.count()
    if match_count == 0:
        try:
            locator.wait_for(timeout=timeout_ms)
            match_count = locator.count()
        except PlaywrightTimeoutError:
            return -1
    return match_count


def copy_resume_on_hh(page: Page, resume: ResumeConfig, dry_run: bool) -> CopyResumeResult:
    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    # ready_selector=RESUME_LIST_CARD: ждём появления хотя бы одной карточки резюме
    # (#142 — раньше готовность проверялась неявно, через wait_for конкретной
    # карточки ниже). Это не заменяет ожидание конкретного resume_id ниже — там
    # нужна именно карточка нужного резюме, а тут сигнал «список отрендерен».
    goto_hh(page, RESUMES_LIST_URL, ready_selector=RESUME_LIST_CARD)

    link_sel = RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume.resume_id)
    card_locator = page.locator(f"{RESUME_LIST_CARD}:has({link_sel})")
    match_count = _wait_single_match_count(card_locator, timeout_ms=COPY_TIMEOUT_MS)
    if match_count == -1:
        return CopyResumeResult(
            resume.id, False, reason=f"резюме {resume.resume_id} не найдено в списке резюме"
        )
    if match_count != 1:
        return CopyResumeResult(
            resume.id,
            False,
            reason=f"карточка резюме {resume.resume_id} определяется неоднозначно "
            f"({match_count} совпадений) — останавливаюсь (fail-closed)",
        )
    card = card_locator.first

    if dry_run:
        logger.info("[DRY-RUN] Скопировал бы резюме '%s' (кнопка меню не нажимается)", resume.id)
        return CopyResumeResult(resume.id, True, reason="dry-run")

    before = _card_hashes(page)

    # Открытие меню «...» ничего не отправляет — WRITE происходит только на
    # клике по «Дублировать» ниже.
    card.locator(RESUME_LIST_ACTION_MORE).click()
    # Скоупим ПОД card, не под page: RESUME_DUPLICATE_INLINE — инлайн-кнопка на
    # каждой карточке, и при нескольких резюме на странице page.locator(...).first
    # взял бы первую в DOM-порядке, а не кнопку открытой карточки — риск скопировать
    # чужое резюме. То же строгое count()-до-wait_for через _wait_single_match_count.
    duplicate_locator = card.locator(f"{RESUME_DUPLICATE_MENU_ITEM}, {RESUME_DUPLICATE_INLINE}")
    dup_count = _wait_single_match_count(duplicate_locator, timeout_ms=COPY_TIMEOUT_MS)
    if dup_count == -1:
        return CopyResumeResult(
            resume.id,
            False,
            reason="кнопка «Дублировать» не найдена: либо достигнут лимит резюме hh.ru "
            "(кнопка при этом не рендерится), либо селектор устарел",
        )
    if dup_count != 1:
        return CopyResumeResult(
            resume.id,
            False,
            reason=f"кнопка «Дублировать» определяется неоднозначно ({dup_count} совпадений "
            "внутри карточки резюме) — останавливаюсь (fail-closed)",
        )
    duplicate = duplicate_locator.first
    duplicate.click()
    logger.info("Клик по «Дублировать» — жду страницу нового резюме")

    new_id = ""
    try:
        page.wait_for_url(_RESUME_HASH_RE, timeout=COPY_TIMEOUT_MS)
        m = _RESUME_HASH_RE.search(page.url)
        if m:
            new_id = m.group(1)
    except PlaywrightTimeoutError:
        logger.warning("Навигация на новое резюме не дождалась — сверяю список резюме")

    if not new_id or new_id == resume.resume_id:
        # Fallback: hh.ru мог увести не на страницу копии — сверяем список до/после.
        # Тот же ready_selector, что при первом открытии (#142): симметрия — список
        # гарантированно отрендерен до _card_hashes ниже.
        goto_hh(page, RESUMES_LIST_URL, ready_selector=RESUME_LIST_CARD)
        created = _card_hashes(page) - before
        if len(created) != 1:
            return CopyResumeResult(
                resume.id,
                False,
                reason=f"hh.ru не подтвердил создание копии (новых резюме в списке: "
                f"{len(created)}) — останавливаюсь (fail-closed)",
            )
        new_id = created.pop()

    if new_id == resume.resume_id:
        return CopyResumeResult(
            resume.id,
            False,
            reason="новый resume_id совпал с исходным — копия не создана (fail-closed)",
        )

    logger.info("Резюме '%s' скопировано, новый resume_id: %s", resume.id, new_id)
    return CopyResumeResult(resume.id, True, new_resume_id=new_id)
