from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import selectors as sel
from .browser import HH_BASE_URL, goto_hh, has_login_form
from .config import ResumeConfig, is_resume_url_placeholder
from .selector_groups.resume_page import RESUME_CARD_LINK_TEMPLATE

logger = logging.getLogger("hhru_bot.bump")

BUMP_TIMEOUT_MS = 10_000
# Короткий таймаут для опционального disabled-hint (#139): элемент — сигнал
# «поднимать рано», он либо отрисуется быстро, либо детерминированно
# отсутствует (кнопка активна). Ждать полный BUMP_TIMEOUT_MS тут не нужно —
# аналогично OPTIONAL_FIELD_TIMEOUT_MS в apply/steps.py.
BUMP_HINT_TIMEOUT_MS = 1_500

# Живой факт 2026-09-08 (census /applicant/resumes): кнопка поднятия мигрировала
# со СТРАНИЦЫ резюме на СПИСОК резюме и живёт внутри карточки конкретного резюме
# (div[data-qa='resume'] > a[data-qa='resume-card-link-<id>']). Прежний поток
# открывал /resume/<id> и не находил кнопку вовсе (healthcheck NOT_FOUND).
RESUMES_LIST_URL = f"{HH_BASE_URL}/applicant/resumes"


@dataclass
class BumpResult:
    resume_id: str
    success: bool
    reason: str = ""
    # #163: реальное действие на hh.ru выполнено (клик по кнопке поднятия).
    # False у всех ранних выходов (плейсхолдер, форма входа, hint «рано»,
    # кнопка не найдена) и у dry-run: на hh.ru не осталось следа, поэтому
    # команда не пишет такие исходы в actions и не ждёт throttle.wait.
    acted: bool = False
    # #176: действие могло выполниться, но результат неизвестен — Playwright
    # бросил исключение во время/сразу после клика (navigation timeout,
    # target closed). fail-closed: acted=True + uncertain=True, чтобы команда
    # гарантированно писала action со статусом 'uncertain' (кулдаун 4ч и
    # дневной лимит его видят) и выдерживала троттл-паузу.
    uncertain: bool = False


def bump_resume(page: Page, resume: ResumeConfig, dry_run: bool) -> BumpResult:
    if is_resume_url_placeholder(resume.resume_url):
        return BumpResult(
            resume.id,
            False,
            "В конфиге указан плейсхолдер resume_url; укажите реальный URL "
            "(получить можно через list-resumes)",
        )
    # Кнопка поднятия живёт на СПИСКЕ резюме (живой факт 2026-09-08, см.
    # RESUMES_LIST_URL выше): карточка резюме резолвится по resume-card-link-<id>,
    # кнопка/hint скоплены внутри неё — на мульти-резюме аккаунта жмём кнопку
    # именно этого резюме, а не первую попавшуюся.
    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    goto_hh(page, RESUMES_LIST_URL)
    if has_login_form(page):
        return BumpResult(
            resume.id,
            False,
            "Сессия недействительна: страница содержит форму входа. Выполните login.",
        )
    card_link = page.locator(RESUME_CARD_LINK_TEMPLATE.format(resume_id=resume.resume_id))
    try:
        card_link.first.wait_for(state="visible", timeout=BUMP_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Раньше этот случай ловил banner-чек на странице резюме (#972); на
        # списке недоступное/удалённое резюме — просто отсутствие карточки.
        # Ранний выход до действия: acted=False (#163), failed/retry.
        return BumpResult(
            resume.id,
            False,
            "резюме не найдено в списке /applicant/resumes (удалено или недоступно)",
        )
    except PlaywrightError:
        # cycle-review #139-паттерн: не-timeout аномалия — fail-closed отказ,
        # а не traceback и не тихий переход к кнопке.
        return BumpResult(
            resume.id, False, "ошибка при поиске карточки резюме в списке — поднятие отменено"
        )
    card = card_link.locator("xpath=ancestor::div[@data-qa='resume'][1]")

    # #139: гонка рендера — раньше hint читался сразу через count() > 0, без
    # ожидания. Непрогрузившаяся страница давала 0 совпадений (не
    # «подсказки нет», а «ещё не отрисовалось»), и код шёл жать кнопку поднятия
    # в обход кулдауна hh.ru. Приводим к тому же приёму, что и кнопка ниже:
    # ждём (короткий таймаут — опциональный элемент), ловим PlaywrightTimeoutError
    # как «hint не появился» = легитимное отсутствие.
    disabled_hint = card.locator(sel.RESUME_BUMP_DISABLED_HINT)
    try:
        disabled_hint.wait_for(state="visible", timeout=BUMP_HINT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    except PlaywrightError:
        # cycle-review #139: не-timeout ошибка (strict-mode violation и т.п.) —
        # аномалия, а не легитимное «hint нет». Раньше пробрасывалась наружу
        # необработанным traceback вместо fail-closed BumpResult; steps.py
        # (эталон) в этой же ситуации явно возвращает отказ, а не падает.
        return BumpResult(
            resume.id, False, "ошибка при проверке подсказки кулдауна — поднятие отменено"
        )
    else:
        return BumpResult(resume.id, False, "hh.ru сообщает, что поднимать ещё рано")

    bump_button = card.locator(sel.RESUME_BUMP_BUTTON)
    try:
        bump_button.wait_for(timeout=BUMP_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return BumpResult(resume.id, False, "кнопка поднятия резюме не найдена на странице")

    if dry_run:
        logger.info("[DRY-RUN] Поднял бы резюме '%s' в поиске", resume.id)
        return BumpResult(resume.id, True, "dry-run")

    # #176: клик по кнопке поднятия — единственное необратимое действие bump.
    # Playwright может бросить исключение уже ПОСЛЕ того, как клик уйдёт на
    # hh.ru (navigation timeout, target closed при редиректе после действия).
    # Проброс исключения наружу рвёт цикл команды ДО record_action/throttle.wait
    # — поднятие на hh.ru произошло, но локальная история об этом не узнала
    # (обход кулдауна 4ч и повторное поднятие). fail-closed в сторону «действие
    # выполнено»: любой PlaywrightError в этой точке = acted+uncertain, запись
    # и пауза гарантированы; ложный «acted» хуже лишь лишней паузой, пропущенный
    # — повторным поднятием для анти-фрода hh.ru.
    try:
        bump_button.click()
    except PlaywrightError as exc:
        logger.warning(
            "Клик поднятия резюме '%s' упал с исключением (%s) — действие могло "
            "уйти на hh.ru, исход считаем неопределённым",
            resume.id,
            exc,
        )
        return BumpResult(
            resume.id,
            False,
            f"клик поднятия выполнен, исход неопределён (Playwright: {exc})",
            acted=True,
            uncertain=True,
        )
    logger.info("Резюме '%s' поднято в поиске", resume.id)
    return BumpResult(resume.id, True, "success", acted=True)
