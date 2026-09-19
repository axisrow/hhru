"""Post-click HH.ru response blockers.

These checks deliberately live between the apply click and form processing.  A
missing response form is not enough evidence to classify the outcome: HH can
render a terminal popup instead.  Selectors are intentionally exact; generic
modal close buttons can close the response form itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..history import SKIP_REASONS
from ..selector_groups import vacancy_page

logger = logging.getLogger(__name__)

# CLAUDE.md п.4: клик по кнопке отклика запускает асинхронный React-рендер,
# поэтому сразу после него DOM ещё пуст.  Терминальные модалки проверяем только
# после короткого явного ожидания — иначе проверка систематически ничего не
# видит.  Таймаут намеренно мал: отсутствие модалки — штатный (частый) случай,
# и ждать её полный APPLY_TIMEOUT_MS на каждой вакансии нельзя.
BLOCKER_RENDER_TIMEOUT_MS = 1_500


@dataclass(frozen=True)
class PostClickBlocker:
    kind: str
    reason: str
    skip_reason: str | None = None
    stop_run: bool = False
    #: True, если блокер найден уже ПОСЛЕ навигации на форму отклика — там
    #: отклик мог физически уйти, поэтому вердикт обязан пройти через
    #: внешнюю проверку #207 (/applicant/negotiations).
    post_navigation: bool = False


class PostSubmitLimitExceeded(RuntimeError):
    """HH rendered the account response limit immediately after submit."""


# #1134 (живой отказ 2026-09-13, аккаунт testing): реальный лимит hh.ru —
# 200 откликов/24ч, отказ рендерится текстом «В течение 24 часов можно
# совершить не более 200 откликов. Вы исчерпали лимит откликов, попробуйте
# отправить отклик позднее.» Строки живут в i18n-словаре страницы
# (vacancy.response.popup.negotiationsLimitExceeded.error), поэтому data-qa
# селектор VACANCY_LIMIT_ERROR (заимствован из reference-проектов, живым DOM
# не подтверждён) в дампах отказа не совпал. Текстовый движок Playwright не
# матчит содержимое <script>, поэтому вездесущий словарный литерал ложных
# срабатываний не даёт; матчится только отрисованный текст отказа.
# ПОДТВЕРЖДЕНО ЖИВЫМ ДАМПОМ 2026-09-14 (apply_137291918_limit_refusal.html,
# боевой прогон): отказ — это НЕ попап с data-qa-popup-error-code, а
# транзиентный Magritte-snackbar (toast, bottom:16px, автоскрытие) с
# generic-контейнером data-qa='snackbar-addon' (общий для всех снекбаров) и
# текстом отказа в .magritte-snackbar-text (id=dialog-description).
# Дискриминатор отказа — ТОЛЬКО текст; автоскрытие toast'а объясняет, почему
# он отсутствует в старых form_timeout-дампах (сняты через 8-14с после клика).
LIMIT_REFUSAL_TEXT_MARKERS = ("не более 200 откликов", "исчерпали лимит откликов")


def limit_refusal_marker(page: Page):
    """Локатор видимого текста отказа по лимиту откликов (#1134)."""

    marker = page.get_by_text(LIMIT_REFUSAL_TEXT_MARKERS[0])
    for extra in LIMIT_REFUSAL_TEXT_MARKERS[1:]:
        marker = marker.or_(page.get_by_text(extra))
    return marker


def limit_refusal_visible(page: Page) -> bool:
    # AttributeError в оговорке: часть тестовых фейков страницы реализует
    # только locator(); для них текстовый детект честно отвечает «отказа нет»,
    # а не роняет весь блокер-проход.
    try:
        return limit_refusal_marker(page).filter(visible=True).first.is_visible()
    except (PlaywrightError, AttributeError):
        return False


def limit_refusal_blocker() -> PostClickBlocker:
    """Определённый вердикт «отказ лимита» без внешней проверки (#1134).

    Текст отказа — это hh.ru, сообщающий, что отклик НЕ принят; в какой бы
    точке клик-зоны он ни увиден (включая серую зону #207 после навигации),
    verify по /applicant/negotiations не нужен: искать нечего, а его
    indeterminate записал бы бессмысленный uncertain и снова заблокировал бы
    вакансию навсегда. stop_run — лимит свойство аккаунта, а не вакансии.
    """

    return PostClickBlocker(
        "limit_exceeded",
        "HH.ru отказал по лимиту откликов (текст отказа в DOM); текущий прогон остановлен",
        stop_run=True,
    )


def _visible(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        return locator.is_visible()
    except (PlaywrightError, AttributeError):
        return False


def _text(page: Page, selector: str) -> str:
    try:
        return page.locator(selector).first.inner_text().lower()
    except (PlaywrightError, AttributeError):
        return ""


def _close_specific(page: Page, selector: str) -> None:
    try:
        locator = page.locator(selector).first
        if locator.is_visible():
            locator.click()
    except (PlaywrightError, AttributeError):
        return


def _wait_for_any_blocker(page: Page, timeout_ms: int) -> None:
    """Даёт модалке отрисоваться до строгих проверок видимости.

    Ждём первый попавшийся из терминальных якорей; отсутствие всех — штатный
    исход (форма отрисовалась нормально), поэтому таймаут проглатывается.
    """

    # ВАЖНО: в Playwright ``timeout=0`` значит «таймаут отключён» (ждать
    # бесконечно), а не «не ждать». Пропуск ожидания выражается только явным
    # ранним выходом, иначе штатная страница без модалки повесила бы прогон.
    if timeout_ms <= 0:
        return
    selector = ", ".join(
        (
            vacancy_page.VACANCY_RELOCATION_CONFIRM,
            vacancy_page.VACANCY_LIMIT_ERROR,
            vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL,
            vacancy_page.VACANCY_RESPONSE_REJECT_WARNING,
            vacancy_page.VACANCY_RESPONSE_ERROR,
            vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE,
        )
    )
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
    except (PlaywrightError, AttributeError):
        return


def relocation_popup_visible(page: Page) -> bool:
    """Видим ли попап запроса подтверждения переезда (#1135).

    Точечная перепроверка для form-timeout пути (steps): общий проход
    ``handle_post_click_blockers`` при ``allow_relocation=true`` КЛИКАЕТ
    подтверждение, поэтому «просто проверить ещё раз» через него нельзя —
    проверка и действие в нём неразделимы.
    """

    return _visible(page, vacancy_page.VACANCY_RELOCATION_CONFIRM)


def handle_post_click_blockers(
    page: Page,
    *,
    allow_relocation: bool,
    render_timeout_ms: int = BLOCKER_RENDER_TIMEOUT_MS,
    post_navigation: bool = False,
    on_relocation_confirmed: Callable[[], None] | None = None,
) -> PostClickBlocker | None:
    """Handle one post-click DOM state and return a terminal blocker if any.

    ``None`` means the caller may continue to form detection.  Similar-vacancy
    overlays are non-terminal and are closed using only their dedicated
    selectors.  Every terminal state is fail-closed and performs no submit.
    """

    _wait_for_any_blocker(page, render_timeout_ms)

    if _visible(page, vacancy_page.VACANCY_RELOCATION_CONFIRM):
        if allow_relocation:
            _close_specific(page, vacancy_page.VACANCY_RELOCATION_CONFIRM)
            # #1135: клик подтверждения мутирует профиль (hh.ru сам меняет сигнал
            # «готов к переезду», боевой факт 2026-09-19) — срабатывание попапа
            # не должно остаться невидимым: лог здесь, маркер в reason успеха
            # через колбэк (см. pipeline._relocation_confirmed_suffix).
            logger.info(
                "Попап запроса подтверждения переезда закрыт кликом (allow_relocation=true, #1135)"
            )
            if on_relocation_confirmed is not None:
                on_relocation_confirmed()
            # Подтверждение переезда — это клик, запускающий свой ре-рендер:
            # следующие строгие проверки нельзя делать по неотстоявшемуся DOM
            # (CLAUDE.md п.4), иначе терминальная модалка будет пропущена.
            _wait_for_any_blocker(page, render_timeout_ms)
        else:
            return PostClickBlocker(
                "relocation_not_allowed",
                "HH запросил подтверждение готовности к переезду; "
                "подтверждение отключено настройками проекта",
                SKIP_REASONS.RELOCATION_NOT_ALLOWED,
                post_navigation=post_navigation,
            )

    if _visible(page, vacancy_page.VACANCY_LIMIT_ERROR):
        return PostClickBlocker(
            "limit_exceeded",
            "HH.ru сообщил об исчерпанном лимите откликов; текущий прогон остановлен",
            stop_run=True,
            post_navigation=post_navigation,
        )

    # #1134: текст отказа — первичный детект (data-qa выше живым DOM не
    # подтверждён и в дампе отказа 2026-09-13 не совпал). Вердикт определённый
    # в любой точке зоны, поэтому post_navigation намеренно НЕ наследуется:
    # verify по #207 для отказа лимита не имеет смысла.
    if limit_refusal_visible(page):
        return limit_refusal_blocker()

    if _visible(page, vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL):
        alert_text = _text(page, vacancy_page.VACANCY_DIRECT_APPLICATION_ALERT)
        if any(marker in alert_text for marker in ("прямым откликом", "сайте работодателя")):
            return PostClickBlocker(
                "direct_application",
                "вакансия требует отклика на сайте работодателя",
                SKIP_REASONS.DIRECT_APPLICATION,
                post_navigation=post_navigation,
            )

    # В отличие от direct-application, здесь текстовый гейт не нужен: оба
    # data-qa специфичны для отклика, тогда как ``magritte-alert`` выше —
    # generic-контейнер дизайн-системы и без проверки текста дал бы ложный скип.
    if _visible(page, vacancy_page.VACANCY_RESPONSE_REJECT_WARNING) or _visible(
        page, vacancy_page.VACANCY_RESPONSE_ERROR
    ):
        return PostClickBlocker(
            "response_rejected",
            "HH.ru показал предупреждение или ошибку отклика",
            SKIP_REASONS.RESPONSE_REJECTED,
            post_navigation=post_navigation,
        )

    # This popup only covers the form; close it and let normal form detection
    # continue.  Never broaden this to a generic close button.
    _close_specific(page, vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE)
    _close_specific(page, 'button:has-text("Не сейчас")')
    return None


def raise_if_post_submit_limit(page: Page) -> None:
    """Raise when HH shows the response limit after the submit click."""
    if _visible(page, vacancy_page.VACANCY_LIMIT_ERROR) or limit_refusal_visible(page):
        raise PostSubmitLimitExceeded(
            "HH.ru сообщил об исчерпанном лимите откликов после submit; текущий прогон остановлен"
        )
