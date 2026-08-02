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
    """
    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    goto_hh(page, RESUMES_LIST_URL)

    cards: list[ResumeCard] = []
    for card in page.locator(RESUME_LIST_CARD).all():
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


def copy_resume_on_hh(page: Page, resume: ResumeConfig, dry_run: bool) -> CopyResumeResult:
    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    goto_hh(page, RESUMES_LIST_URL)

    link_sel = RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume.resume_id)
    card_locator = page.locator(f"{RESUME_LIST_CARD}:has({link_sel})")
    # count() ДО wait_for/click намеренно: Playwright-локаторы строгие — wait_for()
    # на локаторе с >1 совпадением кидает playwright.sync_api.Error ("strict mode
    # violation"), НЕ TimeoutError. Ветка card_locator.count() != 1 ниже проверяет
    # это первой, чтобы неоднозначность ловилась предсказуемо (fail-closed), а не
    # улетала необработанным исключением мимо cli.main (там ловится только
    # KeyboardInterrupt).
    match_count = card_locator.count()
    if match_count == 0:
        try:
            card_locator.wait_for(timeout=COPY_TIMEOUT_MS)
            match_count = card_locator.count()
        except PlaywrightTimeoutError:
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
    # чужое резюме. То же строгое count()-до-wait_for, что и для card_locator выше.
    duplicate_locator = card.locator(f"{RESUME_DUPLICATE_MENU_ITEM}, {RESUME_DUPLICATE_INLINE}")
    dup_count = duplicate_locator.count()
    if dup_count == 0:
        try:
            duplicate_locator.wait_for(timeout=COPY_TIMEOUT_MS)
            dup_count = duplicate_locator.count()
        except PlaywrightTimeoutError:
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
        goto_hh(page, RESUMES_LIST_URL)
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
