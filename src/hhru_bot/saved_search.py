"""Сохранение настроек поиска в автопоиски hh.ru (#1052).

Живые факты (census + разрешённые прогоны 2026-09-08,
scripts/explore_saved_search_1052.py и explore_saved_search_save_1052.py):

- Кнопка «Сохранить поиск» на /search/vacancy открывает Magritte-tooltip
  «Куда присылать новые вакансии по этому поиску?» с кнопками «На почту» /
  «В мессенджер». Поля имени в UI НЕТ — hh.ru именует автопоиск сам (имя =
  text запроса); локальное имя плана — метка CLI, не вводимое значение.
- Клик «На почту» (боевой прогон) сохраняет автопоиск МГНОВЕННО, без
  дополнительного экрана; кнопка переходит в состояние «Сохранён»
  (``SEARCH_SAVE_CREATED``) — позитивный маркер успеха и сигнал дубля.
- Список автопоисков: /applicant/autosearch («Избранное → Поиски»); строка —
  ``favorites-saved-search-item``, имя — aria-label чекбокса, URL выдачи —
  href ссылки-счётчика (``parse_saved_search_url`` разбирает его в
  ``SearchFilters``).

Мутирующий клик — клик по каналу уведомлений; ``before_click`` (seam
``DurableMutationAttempt``) стоит вплотную к нему, а не к безвредному
открытию tooltip'а. Путь через «В мессенджер» боевым прогоном не
исследовался — реализован только email-канал.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, PageStateIndeterminate, goto_hh, has_login_form
from .config import ResumeConfig, SearchFilters
from .responses import NotAuthenticated
from .selector_groups.saved_search import (
    AUTOSEARCH_EMPTY,
    AUTOSEARCH_ITEM,
    AUTOSEARCH_NAME_CHECKBOX,
    AUTOSEARCH_URL_LINK,
    FAVORITES_SEARCHES_TAB,
    SEARCH_SAVE_BUTTON,
    SEARCH_SAVE_CHANNEL_EMAIL,
    SEARCH_SAVE_CREATED,
    SEARCH_SAVE_DROPDOWN,
)

# Страница списка автопоисков hh.ru («Избранное → Поиски»).
AUTOSEARCH_URL = f"{HH_BASE_URL}/applicant/autosearch"

# Гидратация React на /search/vacancy и /applicant/autosearch: после
# ``wait_until="commit"`` body может быть ещё пустым (#858), поэтому перед
# первой строгой проверкой видимости — явный wait (паттерн CLAUDE.md,
# «commit не значит отрисовано»). Бюджет свой для каждого экрана.
SEARCH_BAR_RENDER_TIMEOUT_MS = 15_000
AUTOSEARCH_RENDER_TIMEOUT_MS = 15_000


class SavedSearchListIndeterminate(PageStateIndeterminate):
    """Список автопоисков не удалось подтвердить (строки не прочитаны).

    ``state`` — словарь состояний browser.PAGE_STATE (см. VacancySearchIndeterminate).
    """

    def __init__(self, message: str, *, state: str = "indeterminate"):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class SavedSearchPlan:
    """План сохранения: что именно уйдёт в автопоиск hh.ru."""

    name: str
    search_url: str
    params: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SaveSearchResult:
    """Итог ``save_search_on_hh`` — структурные флаги для ApplyProgress.finish."""

    query_key: str
    name: str
    reason: str
    success: bool
    acted: bool = False
    uncertain: bool = False
    skipped: bool = False


@dataclass
class SavedSearchEntry:
    """Один автопоиск из списка аккаунта."""

    name: str
    # URL выдачи автопоиска, если hh.ru отдал его в списке. None — структура
    # строки не подтверждена живым DOM (сейчас всегда так: непустой список
    # живым замером не наблюдался).
    url: str | None = None


def saved_search_params(filters: SearchFilters) -> list[tuple[str, str]]:
    """Непустые канонические параметры поиска в стабильном порядке.

    Это ровно тот набор, который ``build_search_url`` кладёт в URL выдачи:
    автопоиск hh.ru — сохранённые query-параметры, ничего сверх них в план
    не добавляется («ничего не выдумывать», #1052).
    """
    params: list[tuple[str, str]] = [("text", filters.text)]
    if filters.area is not None:
        params.append(("area", str(filters.area)))
    if filters.salary_from is not None:
        params.append(("salary", str(filters.salary_from)))
    if filters.experience is not None:
        params.append(("experience", filters.experience))
    if filters.schedule is not None:
        params.append(("schedule", filters.schedule))
    return params


def deterministic_saved_search_name(filters: SearchFilters) -> str:
    """Имя автопоиска из параметров, без выдуманных значений.

    ``text`` плюс ``ключ=значение`` остальных непустых параметров. Одинаковым
    параметрикам соответствует одинаковое имя — дубль того же запроса виден
    в списке автопоисков ещё до попытки сохранения.
    """
    params = saved_search_params(filters)
    head, *rest = params
    name = head[1]
    if rest:
        name += " (" + ", ".join(f"{k}={v}" for k, v in rest) + ")"
    return name


def saved_search_query_key(filters: SearchFilters) -> str:
    """Детерминированный локальный ключ параметрики (слот resume_id истории).

    Автопоиск не привязан к резюме, поэтому для actions-строки используется
    ключ параметрики, а не resume_id.
    """
    canonical = urlencode(saved_search_params(filters))
    return f"savedsearch:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def build_saved_search_url(filters: SearchFilters) -> str:
    """URL выдачи, параметрика которой сохраняется как автопоиск."""
    from .search import build_search_url

    return build_search_url(filters)


def list_saved_searches(page: Page) -> list[SavedSearchEntry]:
    """Прочитать список автопоисков аккаунта (read-only, /applicant/autosearch).

    Пустой список подтверждается ``AUTOSEARCH_EMPTY`` (#464). Строка
    автопоиска (боевой readback 2026-09-08): имя — aria-label чекбокса,
    URL выдачи — href ссылки-счётчика; оба селектора live-подтверждены.
    Строка без читаемого имени — ``SavedSearchListIndeterminate``.
    """
    try:
        goto_hh(page, AUTOSEARCH_URL)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise SavedSearchListIndeterminate(
            f"страница автопоисков не открылась: {exc}", state="unreachable"
        ) from exc

    if has_login_form(page):
        raise NotAuthenticated("страница автопоисков содержит форму входа — сессия отвергнута")

    try:
        page.locator(FAVORITES_SEARCHES_TAB).first.wait_for(
            state="visible", timeout=AUTOSEARCH_RENDER_TIMEOUT_MS
        )
        empty_count = page.locator(AUTOSEARCH_EMPTY).count()
        items = page.locator(AUTOSEARCH_ITEM)
        item_count = items.count()
        entries: list[SavedSearchEntry] = []
        for i in range(item_count):
            item = items.nth(i)
            name = item.locator(AUTOSEARCH_NAME_CHECKBOX).first.get_attribute("aria-label")
            href = item.locator(AUTOSEARCH_URL_LINK).first.get_attribute("href")
            if not name or not name.strip():
                raise SavedSearchListIndeterminate(
                    f"строка автопоиска #{i + 1} без читаемого имени (aria-label чекбокса пуст)",
                    state="indeterminate",
                )
            entries.append(SavedSearchEntry(name=name.strip(), url=href))
        if empty_count == 1 and not entries:
            return []
        if not entries and empty_count != 1:
            raise SavedSearchListIndeterminate(
                "список автопоисков не прочитан: нет ни строк, ни подтверждённого "
                "пустого состояния (возможен дрейф селекторов, #1052)",
                state="indeterminate",
            )
        return entries
    except SavedSearchListIndeterminate:
        raise
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise SavedSearchListIndeterminate(
            f"список автопоисков не прочитан: {exc}", state="indeterminate"
        ) from exc


def find_duplicate_saved_search(
    entries: list[SavedSearchEntry], filters: SearchFilters
) -> SavedSearchEntry | None:
    """Найти автопоиск с той же параметрикой поиска (дубль, #1052).

    Кнопка «Сохранён» неперсистентна (живой census 2026-09-08: в новой сессии
    на точно сохранённой параметрике hh.ru снова показывает «Сохранить
    поиск»), поэтому надёжный дубль-детект — сравнение параметрики из URL
    строк списка автопоисков с планом сохранения.
    """
    wanted = saved_search_params(filters)
    for entry in entries:
        if entry.url and saved_search_params(parse_saved_search_url(entry.url)) == wanted:
            return entry
    return None


def parse_saved_search_url(url: str) -> SearchFilters:
    """Разобрать ссылку выдачи автопоиска в ``SearchFilters``.

    Берутся только параметры, которые строит ``build_search_url`` (text,
    area, salary, experience, schedule); сервисные (saved_search_id и пр.)
    игнорируются. Незнакомых параметров настройки поиска CLI в URL не
    бывает — локальные фильтры (стоп-списки и т.п.) в автопоиск не входят.
    """
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    text = (query.get("text") or [""])[0]

    def _int(key: str) -> int | None:
        raw = (query.get(key) or [None])[0]
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    return SearchFilters(
        text=text,
        area=_int("area"),
        salary_from=_int("salary"),
        experience=(query.get("experience") or [None])[0],
        schedule=(query.get("schedule") or [None])[0],
    )


def save_search_on_hh(
    page: Page,
    resume: ResumeConfig,
    name: str | None,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> SaveSearchResult:
    """Сохранить параметрику поиска резюме как автопоиск hh.ru.

    ``dry_run=True``: открыть выдачу, подтвердить кнопку сохранения и
    вернуть план (имя, URL, параметры) — без единого клика.

    Боевой путь (живой прогон 2026-09-08): клик по кнопке открывает tooltip
    канала уведомлений; клик «На почту» — мутирующий, ``before_click``
    вызывается вплотную к нему. Успех — позитивный маркер «Сохранён»;
    таймаут после клика канала — ``uncertain`` (клик мог уйти, #176/#207).
    Если кнопка уже в состоянии «Сохранён» — ``skipped`` (дубль).
    """
    filters = resume.search
    resolved_name = name or deterministic_saved_search_name(filters)
    query_key = saved_search_query_key(filters)
    url = build_saved_search_url(filters)
    plan = SavedSearchPlan(
        name=resolved_name,
        search_url=url,
        params=saved_search_params(filters),
    )

    try:
        goto_hh(page, url)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return SaveSearchResult(
            query_key,
            resolved_name,
            success=False,
            reason=f"выдача поиска не открылась: {exc}",
        )

    if has_login_form(page):
        raise NotAuthenticated("страница поиска содержит форму входа — сессия отвергнута")

    # Строка поиска несёт кнопку «Сохранить поиск», которая СРАЗУ после
    # успешного сохранения (в той же сессии) переходит в состояние «Сохранён»
    # (SEARCH_SAVE_CREATED) — боевой прогон 2026-09-08. Состояние
    # неперсистентно (новая сессия снова показывает «Сохранить поиск» даже на
    # сохранённой параметрике), поэтому надёжный дубль-детект — в команде, по
    # списку автопоисков; здесь CREATED — только маркер немедленного успеха.
    # Гидратационный race (#858): строгие проверки count() — после wait_for.
    try:
        buttons = page.locator(SEARCH_SAVE_BUTTON)
        try:
            buttons.first.wait_for(state="visible", timeout=SEARCH_BAR_RENDER_TIMEOUT_MS)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            # Кнопки нет: либо дубль (кнопка в состоянии «Сохранён»), либо
            # невырисованная страница/дрейф селектора — различаем по CREATED.
            try:
                created_count = page.locator(SEARCH_SAVE_CREATED).count()
            except (PlaywrightTimeoutError, PlaywrightError):
                created_count = 0
            if created_count == 1:
                return SaveSearchResult(
                    query_key,
                    resolved_name,
                    success=False,
                    skipped=True,
                    reason="этот поиск уже сохранён в автопоиски (кнопка в состоянии «Сохранён»)",
                )
            return SaveSearchResult(
                query_key,
                resolved_name,
                success=False,
                reason=f"кнопка «Сохранить поиск» не подтвердилась: {exc}",
            )
        count = buttons.count()
        if count != 1:
            return SaveSearchResult(
                query_key,
                resolved_name,
                success=False,
                reason=f"кнопка «Сохранить поиск» неоднозначна или не найдена (найдено {count})",
            )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return SaveSearchResult(
            query_key,
            resolved_name,
            success=False,
            reason=f"кнопка «Сохранить поиск» не подтвердилась: {exc}",
        )

    if dry_run:
        param_line = ", ".join(f"{k}={v}" for k, v in plan.params)
        return SaveSearchResult(
            query_key,
            resolved_name,
            success=True,
            reason=f"[DRY-RUN] план автопоиска: имя='{plan.name}', параметры: {param_line}",
        )

    # Боевой путь (живой прогон 2026-09-08): клик по кнопке открывает tooltip
    # выбора канала уведомлений; клик «На почту» сохраняет автопоиск мгновенно,
    # без дополнительного экрана, и кнопка переходит в «Сохранён». Мутирующий
    # клик — именно клик по каналу, поэтому before_click (seam
    # DurableMutationAttempt) стоит вплотную к нему, не к безвредному открытию.
    try:
        page.locator(SEARCH_SAVE_BUTTON).first.click()
        page.locator(SEARCH_SAVE_DROPDOWN).first.wait_for(
            state="visible", timeout=SEARCH_BAR_RENDER_TIMEOUT_MS
        )
        channels = page.locator(SEARCH_SAVE_CHANNEL_EMAIL)
        if channels.count() != 1:
            return SaveSearchResult(
                query_key,
                resolved_name,
                success=False,
                reason=f"кнопка канала «На почту» неоднозначна или не найдена "
                f"(найдено {channels.count()})",
            )
        if before_click is not None:
            before_click()
        channels.first.click()
        page.locator(SEARCH_SAVE_CREATED).first.wait_for(
            state="visible", timeout=SEARCH_BAR_RENDER_TIMEOUT_MS
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        # После before_click клик мог уйти — исход uncertain, fail-closed (#176).
        return SaveSearchResult(
            query_key,
            resolved_name,
            success=False,
            acted=True,
            uncertain=True,
            reason=f"клик канала выполнен, но состояние «Сохранён» не подтвердилось: {exc}",
        )

    return SaveSearchResult(
        query_key,
        resolved_name,
        success=True,
        acted=True,
        reason="автопоиск сохранён (кнопка в состоянии «Сохранён»); имя назначает hh.ru",
    )
