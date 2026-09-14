"""Многостраничное чтение /applicant/my_resumes (#1133).

Список резюме аккаунта может занимать несколько страниц; раньше и
``delete-resume``, и ``list-resumes`` читали только первую, из-за чего
карточки со второй страницы «не существовали» (ложный «карточка не
появилась» при живой сессии, RUN a1bd13d6, 2026-09-13).

Пагинация — UI-кликом по ``pager-next`` (как человек; навигационный клик,
никаких прямых запросов). Селекторы pager-семейства ``data-qa='pager-*``
подтверждены живым DOM на странице поиска вакансий (search.py #123) и в
переговорах (negotiations); на /applicant/my_resumes они НЕ наблюдались —
на testing-аккаунте список одностраничен, пагинатор не рендерится
(census 2026-09-14, data/logs/census_list_pagination.json). Риск
асимметричен, как у пост-кликовых блокеров (#342): отсутствие селектора
читается как «последняя страница» (безопасный фолбэк к прежнему
одностораничному поведению), ложное совпадение только продлило бы
чтение. Кликом управляет DOM: нет ``pager-next`` — чтение закончено.

Потолок страниц — ``MAX_LIST_PAGES``: SSR списка отдаёт
``resumeLimits.max`` (20 на живом аккаунте 2026-09-14), то есть при
типичной странице в 10 карточек страниц не больше двух; потолок —
fail-closed предохранитель от бесконечного цикла на дрейфе разметки,
а не оценка размера аккаунта.
"""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .resume_ids import resume_card_locator
from .selector_groups.search_page import PAGINATION_NEXT

MAX_LIST_PAGES = 10

# Ждём смены URL после клика по pager-next. ``commit`` — единственное
# значение, не требующее lifecycle-события документа (#179); у SPA-перехода
# (pushState) смена URL подтверждается им же.
_NEXT_PAGE_NAV_TIMEOUT_MS = 15_000


def turn_list_page(page: Page) -> str:
    """Открыть следующую страницу списка /applicant/my_resumes.

    Возвращает ``""`` при подтверждённом переходе, ``"last"`` если
    ``pager-next`` отсутствует (текущая страница последняя), иначе строку
    с причиной (клик не подтвердился сменой URL за бюджет — состояние
    пагинации не доказано, продолжать нельзя).
    """
    next_button = page.locator(PAGINATION_NEXT)
    if next_button.count() == 0:
        return "last"
    url_before = page.url
    try:
        next_button.first.click()
        page.wait_for_url(
            lambda url: str(url) != url_before,
            wait_until="commit",
            timeout=_NEXT_PAGE_NAV_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        return f"переход по pager-next не подтвердился: {exc}"
    return ""


def find_resume_card(
    page: Page, resume_id: str, *, wait_attached_ms: int
) -> tuple[Locator | None, str]:
    """Найти identity-карточку резюме на текущей и последующих страницах.

    Возвращает ``(locator, "")`` при ровно одном совпадении; ``(None, "")``
    если страницы списка закончились (карточки нет — при подтверждённом
    DOM); ``(None, reason)`` когда список не дочитан или карточка
    неоднозначна — вызывающий обязан отличать «нет» от «не дочитали».
    """
    for _page_index in range(MAX_LIST_PAGES):
        card = resume_card_locator(page, resume_id)
        if card.count() == 0:
            # Ждём отрисовку на КАЖДОЙ странице: после перехода по pager-next
            # счётчик карточек читается до гидратации новой страницы — без
            # ожидания недогруженная страница выглядела бы последней
            # (таймаут — вызывающего).
            try:
                card.first.wait_for(state="attached", timeout=wait_attached_ms)
            except PlaywrightError:
                pass
        if card.count() == 1:
            return card, ""
        if card.count() > 1:
            return None, f"карточка resume_id={resume_id} не подтверждена однозначно"
        turn_error = turn_list_page(page)
        if turn_error == "last":
            return None, ""
        if turn_error:
            return None, (
                f"список резюме не дочитан: {turn_error} — «резюме нет» утверждать нельзя"
            )
    return None, (
        f"список резюме не дочитан: превышен потолок {MAX_LIST_PAGES} страниц "
        "— «резюме нет» утверждать нельзя"
    )


def collect_resume_cards_over_pages(page: Page, collect_page) -> list:
    """Собрать карточки со всех страниц списка (``collect_page(page)``).

    ``collect_page`` — постраничный читатель без навигации (у
    ``list-resumes`` это ``copy_resume.list_resume_cards(page,
    navigate=False)`` с его SSR-статусами и fail-closed таксономией).
    Любая строка-причина ``turn_list_page`` кроме ``"last"`` — читатель
    уже вернул непустой список текущей страницы; остановка с тем, что
    прочитано, честнее потерянных карточек, но вызывающий теряет
    гарантию полноты — поэтому непоследняя страница с несменившимся URL
    поднимает исключение вызывающего контракта.

    Честный риск, документирован в PR #1143: если переходы по pager на
    my_resumes окажутся SPA-переходами (pushState без загрузки документа),
    SSR-статусы страницы N+1 останутся от начальной загрузки — постраничный
    читатель тогда вернёт статусы не тех карточек. До живого прогона на
    многостраничном аккаунте это не проверить.

    Контракт ``collect_page``: читатель сам ждёт готовность страницы после
    перехода (правило «commit не значит отрисовано»); ``list_resume_cards``
    ему удовлетворяет — внутри ``wait_for`` карточек с бюджетом и честный
    ``ResumeListIndeterminate`` вместо пустого списка.
    """
    collected: list = []
    for _ in range(MAX_LIST_PAGES):
        collected.extend(collect_page(page))
        turn_error = turn_list_page(page)
        if turn_error == "last":
            return collected
        if turn_error:
            # Частичный список недостоверен (#320): непрочитанные страницы
            # нельзя выдавать за полный список аккаунта.
            raise RuntimeError(
                f"список резюме не дочитан: {turn_error} — прочитано {len(collected)} карточек"
            )
    raise RuntimeError(
        f"список резюме не дочитан: превышен потолок {MAX_LIST_PAGES} страниц "
        f"(прочитано {len(collected)} карточек)"
    )


__all__ = [
    "MAX_LIST_PAGES",
    "collect_resume_cards_over_pages",
    "find_resume_card",
    "turn_list_page",
]
