"""Browser step for reporting a vacancy on hh.ru (issue #745).

Разведка issue #745 подтвердила живым DOM (2026-08-29, vacancy_id=136672001,
дамп и полный перечень причин в теле PR) только первые два шага
трёхшагового bloko-modal wizard'а "Пожаловаться на вакансию":

  1. выбор причины (радио, ``VACANCY_COMPLAIN_REASON_IDS``);
  2. комментарий (обязателен для ВСЕХ причин без исключения — commentRequired
     в SSR JSON).

Шаг 3 (финальное подтверждение отправки) НЕ исследован и НЕ будет: issue
прямо запрещает клик, ведущий к реальной отправке жалобы (необратимо, видно
работодателю, уходит от лица пользователя). Поэтому эта функция всегда
останавливается сразу после шага 2 с ``success=False`` — тот же fail-closed
контракт, что у `clear_negotiations`/`apply` для непроверенных терминальных
кликов: нет подтверждённого элемента дальше этой точки, значит команда не
идёт дальше сама.

**Постоянная блокировка `has_unresolved_uncertain` (CLAUDE.md, раздел 6,
паттерн #480/delete-resume, но с другим инвариантом).** Поскольку `success`
здесь никогда не бывает `True`, `uncertain`-строка, записанная
`DurableMutationAttempt.interrupt()` при исключении ПОСЛЕ `before_click()`
(клик по кнопке «Ещё»), заблокирует `report-vacancy` для этого `vacancy_id`
навсегда — не «пока не подтверждён», а структурно недостижимо. В отличие от
`delete-resume`, здесь reconciliation не требует проверки состояния на hh.ru:
ни один клик до (не включая) неисследованного шага 3 не мутирует hh.ru
(вся форма до заполненного комментария — чисто клиентский рендер), поэтому
можно безусловно вставить резолюцию:
```sql
INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at)
VALUES ('<vacancy_id>', '<vacancy_id>', 'report_vacancy', 'success',
        'manual reconciliation: no hh.ru mutation occurred before step 3',
        datetime('now'));
```
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh, has_login_form
from .responses import NotAuthenticated
from .selector_groups.vacancy_complain import (
    VACANCY_COMPLAIN_COMMENT_TEXTAREA,
    VACANCY_COMPLAIN_MENU_ITEM,
    VACANCY_COMPLAIN_MODAL,
    VACANCY_COMPLAIN_PAGE_BUTTON,
    VACANCY_COMPLAIN_REASON_IDS,
    VACANCY_COMPLAIN_REASON_RADIO,
    VACANCY_COMPLAIN_WIZARD_NEXT,
    VACANCY_MORE_ACTIONS,
)

MENU_OPEN_TIMEOUT_MS = 10_000
PAGE_BUTTON_TIMEOUT_MS = 10_000
MODAL_TIMEOUT_MS = 10_000


@dataclass
class ReportVacancyResult:
    vacancy_id: str
    reason: str
    success: bool
    reason_text: str
    uncertain: bool = False


def report_vacancy_on_hh(
    page: Page,
    vacancy_id: str,
    reason: str,
    comment: str,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> ReportVacancyResult:
    """Navigate the complaint wizard up to step 2 (comment) and stop.

    Never submits the complaint. ``success`` is always ``False`` because the
    only confirmed, safe outcome of this read+write exploration is reaching
    a filled, ready-to-submit step 2 — the actual submit control (step 3) was
    never clicked in the field and is intentionally out of scope (see module
    docstring). Callers must treat a non-success result as normal, not as a
    bug: the command's job stops at "verified reachable", the human decides
    whether to press submit manually in a real browser.

    ``before_click`` is called exactly once, right before the first UI click,
    to durably reserve the actions-ledger row (same seam as
    ``DurableMutationAttempt.before_click`` used by other WRITE-hh-ru
    commands) — even though no click in this flow reaches hh.ru's server
    before the unimplemented step 3, keeping the same reserve-before-click
    seam avoids a second, inconsistent contract for this one command.
    """
    if reason not in VACANCY_COMPLAIN_REASON_IDS:
        return ReportVacancyResult(
            vacancy_id,
            reason,
            success=False,
            reason_text=(
                f"причина '{reason}' не входит в подтверждённый перечень "
                f"({', '.join(VACANCY_COMPLAIN_REASON_IDS)})"
            ),
        )

    url = f"{HH_BASE_URL}/vacancy/{vacancy_id}"
    try:
        goto_hh(page, url)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return ReportVacancyResult(
            vacancy_id, reason, success=False, reason_text=f"страница вакансии не открылась: {exc}"
        )

    if has_login_form(page):
        raise NotAuthenticated("страница содержит форму входа — сессия отвергнута")

    more_btn = page.locator(VACANCY_MORE_ACTIONS).locator("visible=true")
    try:
        more_btn.first.wait_for(state="visible", timeout=MENU_OPEN_TIMEOUT_MS)
    except (PlaywrightTimeoutError, PlaywrightError):
        return ReportVacancyResult(
            vacancy_id, reason, success=False, reason_text="кнопка 'Ещё' не найдена или не видна"
        )
    if more_btn.count() != 1:
        return ReportVacancyResult(
            vacancy_id,
            reason,
            success=False,
            reason_text=f"кнопка 'Ещё' неоднозначна (видимых совпадений: {more_btn.count()})",
        )

    if dry_run:
        return ReportVacancyResult(
            vacancy_id,
            reason,
            success=False,
            reason_text=(
                f"[DRY-RUN] дошёл бы до формы жалобы, причина={reason}, "
                f"комментарий={comment!r} (без клика)"
            ),
        )

    if before_click is not None:
        before_click()

    try:
        more_btn.first.click()
        menu_item = page.locator(VACANCY_COMPLAIN_MENU_ITEM)
        menu_item.first.wait_for(state="visible", timeout=MENU_OPEN_TIMEOUT_MS)
        if menu_item.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=f"пункт 'Пожаловаться' неоднозначен (совпадений: {menu_item.count()})",
            )
        if menu_item.first.is_disabled():
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=(
                    "жалоба на эту вакансию уже была отправлена этим аккаунтом "
                    "('Оставили жалобу', кнопка disabled) — не кликаю"
                ),
            )

        menu_item.first.click()
        page_btn = page.locator(VACANCY_COMPLAIN_PAGE_BUTTON)
        page_btn.first.wait_for(state="visible", timeout=PAGE_BUTTON_TIMEOUT_MS)
        if page_btn.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=(
                    f"реальный триггер формы жалобы неоднозначен (совпадений: {page_btn.count()})"
                ),
            )

        # Этот клик открывает bloko-modal wizard — подтверждено разведкой как
        # ЧИСТО КЛИЕНТСКИЙ рендер (никакого запроса на hh.ru до шага 3).
        # Настоящая "точка невозврата" issue #207/#476 здесь ещё не наступила:
        # она наступает только при клике по несуществующему в этом коде шагу 3
        # (финальная отправка). Поэтому все проверки ниже, включая исключения,
        # остаются обычным failed, а не uncertain — ни один из этих кликов не
        # мог уйти на сервер, uncertain здесь был бы неоправданной постоянной
        # блокировкой has_unresolved_uncertain для вакансии без реального риска.
        page_btn.first.click()
        modal = page.locator(VACANCY_COMPLAIN_MODAL)
        modal.first.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
        if modal.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=f"модалка жалобы неоднозначна (совпадений: {modal.count()})",
            )

        reason_radio = page.locator(VACANCY_COMPLAIN_REASON_RADIO.format(reason=reason))
        if reason_radio.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=(
                    f"радио причины '{reason}' не найдено однозначно "
                    f"(совпадений: {reason_radio.count()})"
                ),
            )
        # bloko-radio: <input> визуально скрыт под своим <label> — клик по
        # самому input перекрыт и падает по таймауту (подтверждено разведкой).
        reason_radio.first.locator("xpath=ancestor::label[contains(@class,'bloko-radio')]").click()

        next_btn = page.locator(VACANCY_COMPLAIN_WIZARD_NEXT)
        if next_btn.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=f"кнопка 'Продолжить' неоднозначна (совпадений: {next_btn.count()})",
            )
        if next_btn.first.is_disabled():
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text="кнопка 'Продолжить' осталась disabled после выбора причины",
            )
        next_btn.first.click()

        comment_field = page.locator(VACANCY_COMPLAIN_COMMENT_TEXTAREA)
        comment_field.first.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
        if comment_field.count() != 1:
            return ReportVacancyResult(
                vacancy_id,
                reason,
                success=False,
                reason_text=f"поле комментария неоднозначно (совпадений: {comment_field.count()})",
            )
        comment_field.first.fill(comment)

        next_btn2 = page.locator(VACANCY_COMPLAIN_WIZARD_NEXT)
        disabled_after_fill = next_btn2.count() != 1 or next_btn2.first.is_disabled()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        # Ни один шаг wizard'а до (не включая) несуществующего в этом коде
        # шага 3 не мутирует hh.ru — обычный failed, не uncertain.
        return ReportVacancyResult(
            vacancy_id, reason, success=False, reason_text=f"состояние формы не подтверждено: {exc}"
        )

    # Шаг 2 достигнут и заполнен — это НАМЕРЕННЫЙ конечный пункт команды
    # (см. module docstring). Дальше — шаг 3, неисследованный и запрещённый
    # к клику issue #745. success остаётся False: это не сбой, это дизайн.
    return ReportVacancyResult(
        vacancy_id,
        reason,
        success=False,
        reason_text=(
            "форма жалобы заполнена (причина + комментарий), кнопка 'Продолжить' "
            f"disabled={disabled_after_fill}; шаг 3 (финальная отправка) НЕ "
            "исследован и не кликается — жалоба НЕ отправлена. Завершите вручную "
            "в обычном браузере, если действительно хотите отправить."
        ),
        uncertain=False,
    )
