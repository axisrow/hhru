"""Сохранение настроек поиска в автопоиски hh.ru (#1052).

Разведка живым DOM 2026-09-08 (census + один разрешённый клик по кнопке,
scripts/explore_saved_search_1052.py): кнопка «Сохранить поиск» на
/search/vacancy НЕ мутирует hh.ru — она открывает Magritte-tooltip
«Куда присылать новые вакансии по этому поиску?» с кнопками «На почту» /
«В мессенджер» (``SEARCH_SAVE_CHANNEL_*``). Поля имени в UI НЕТ: hh.ru
именует автопоиск сам, поэтому локальное имя плана — метка CLI для плана
и дедупа, а не вводимое значение. Мутирующая граница — клик по каналу
уведомлений и следующий за ним не исследованный экран; без боевого
разрешения он не выполняется, боевой путь честно отказывается ДО клика
(тот же fail-closed контракт, что у ``report_vacancy`` #745).

Список автопоисков: /applicant/autosearch («Избранное → Поиски»).
Подтверждено только пустое состояние списка (``AUTOSEARCH_EMPTY``); строки
непустого списка живым замером не наблюдались, поэтому их чтение —
``SavedSearchListIndeterminate``, а не пустой список (паттерн #464: пустой
результат требует подтверждения состояния страницы).
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
    FAVORITES_SEARCHES_TAB,
    SEARCH_SAVE_BUTTON,
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

    Пустой список подтверждается ``AUTOSEARCH_EMPTY``. Непустой список
    пока не наблюдаем живьём: строки не парсятся и не выдаются за пустые —
    ``SavedSearchListIndeterminate`` с честной причиной.
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
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise SavedSearchListIndeterminate(
            f"вкладка «Поиски» не подтвердилась: {exc}", state="indeterminate"
        ) from exc

    try:
        empty_count = page.locator(AUTOSEARCH_EMPTY).count()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise SavedSearchListIndeterminate(
            f"состояние списка автопоисков не прочитано: {exc}", state="indeterminate"
        ) from exc
    if empty_count == 1:
        return []

    raise SavedSearchListIndeterminate(
        "список автопоисков непуст, но селекторы его строк не подтверждены живым DOM "
        "(сняты census только кнопки сохранения и пустого состояния, #1052); "
        "снимите census непустого списка и дополните selector_groups/saved_search.py",
        state="indeterminate",
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

    Боевой путь: НЕ КЛИКАЕТ. Разведка 2026-09-08 показала, что клик по
    кнопке открытия безвреден, но мутирующая граница — выбор канала
    уведомлений («На почту»/«В мессенджер») и следующий за ним
    не исследованный экран: за ней локальные таймауты ничего не доказывают
    (#207), а подтвердить результат нечем. Fail-closed отказ ``acted=False``
    — то же правило, что у report_vacancy (#745). ``before_click``
    зарезервирован для будущего боевого пути (seam DurableMutationAttempt),
    сейчас не вызывается никогда.
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

    try:
        buttons = page.locator(SEARCH_SAVE_BUTTON)
        count = buttons.count()
        if count > 1:
            return SaveSearchResult(
                query_key,
                resolved_name,
                success=False,
                reason=f"кнопка «Сохранить поиск» неоднозначна (найдено {count})",
            )
        if count == 0:
            return SaveSearchResult(
                query_key,
                resolved_name,
                success=False,
                reason="кнопка «Сохранить поиск» не найдена на выдаче",
            )
        buttons.first.wait_for(state="visible", timeout=SEARCH_BAR_RENDER_TIMEOUT_MS)
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

    return SaveSearchResult(
        query_key,
        resolved_name,
        success=False,
        acted=False,
        reason=(
            "мутирующая граница автопоиска — выбор канала уведомлений "
            "(«На почту»/«В мессенджер») и следующий за ним экран; они не исследованы "
            "боевым прогоном (#1052), поэтому клик не выполняется. UI имени не "
            "запрашивает: hh.ru именует автопоиск сам (локальное имя плана — метка CLI)"
        ),
    )
