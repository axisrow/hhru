"""Read and resolve hh.ru professional roles through the live UI.

This module owns the **vacancy search catalog** (``vacancy_search_catalog``):
the professional-role filter of ``/search/vacancy``, opened read-only.  It is
the single source of ``role_id`` for ``--specialization`` validation.

It is NOT the resume wizard's professional-role screen
(``resume_wizard_roles``, ``/profile/resume/professional_role``): that screen
is a fixed list of text categories with no ``role_id`` at all (#904).  The
two must never share a name — the conflated wording sent #894-#898 hunting
for a catalog on the chip screen, where none exists (#908).  The only wizard
surface whose ids match this catalog is the «Уточните специальность» modal
tree (#913).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh, wait_for_named_control_hydration
from .external_forms.detect import normalize

logger = logging.getLogger(__name__)

SEARCH_URL = f"{HH_BASE_URL}/search/vacancy"
FILTER_TRIGGER = "[data-qa='search-filter-professional-role-trigger']"
TREE_INPUT = "[data-qa~='tree-selector-input-{}']"
TREE_INPUT_ANY = "input[data-qa*='tree-selector-input-']"
TREE_LABEL = "[data-qa='cell-text-content']"
_ROLE_ID_RE = re.compile(r"(?:^|\s)tree-selector-input-(\d+)(?:\s|$)")
_CATEGORY_ID_RE = re.compile(r"(?:^|\s)tree-selector-input-category-(\d+)(?:\s|$)")
_WAIT_MS = 15_000
_MAX_SCROLL_STEPS = 200
# #1004: ожидание факта «leaf-строки появились/исчезли» после клика по шеврону.
_LEAF_ATTACH_TIMEOUT_MS = 5_000
_COLLAPSE_POLL_MS = 100
_COLLAPSE_ATTEMPTS = 3
# #858/#1004: окно гидрации тоггла «Фильтры» (SSR-кнопка видима до React).
_FILTERS_HYDRATION_TIMEOUT_MS = 15_000

CACHE_SCHEMA_VERSION = 1
CACHE_SOURCE = SEARCH_URL
CACHE_LOCALE = "ru"
CACHE_MAX_AGE = timedelta(days=7)
DEFAULT_CACHE_PATH = Path("data/cache/professional_roles.json")


@dataclass(frozen=True)
class ProfessionalRole:
    role_id: str
    label: str
    category: str = ""
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfessionalRoleCategory:
    category_id: str
    label: str


@dataclass(frozen=True)
class VacancySearchRoleCatalog:
    """Snapshot of the vacancy-search role filter (#908) — the ``role_id``
    source.  Not the resume wizard chip screen, which has no ids at all."""

    fetched_at: datetime
    categories: tuple[str, ...]
    roles: tuple[ProfessionalRole, ...]


class ProfessionalRoleCacheError(RuntimeError):
    """The local vacancy-search catalog snapshot is missing, malformed, or
    incomplete."""


def build_role_query_prompt(title: str) -> list[dict[str, str]]:
    """Ask for a few Russian vacancy-search catalog query phrases, not a classification."""
    return [
        {
            "role": "system",
            "content": (
                "Подбери 1-4 коротких русских поисковых запроса для live-каталога "
                "профессий hh.ru (фильтры поиска вакансий /search/vacancy). "
                "Не выбирай профессию и не выдумывай каталог. "
                'Ответь только JSON: {"queries":["..."]}.'
            ),
        },
        {"role": "user", "content": title},
    ]


def _strip_json_fence(raw: str) -> str:
    if raw.startswith("```"):
        return raw.strip("`").removeprefix("json").strip()
    return raw


def parse_role_queries(content: str | None) -> list[str]:
    if not content or not content.strip():
        raise ValueError("LLM не предложил запросы к каталогу поиска вакансий")
    raw = _strip_json_fence(content.strip())
    data = json.loads(raw)
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or not all(isinstance(item, str) for item in queries):
        raise ValueError("LLM-запросы к каталогу поиска вакансий должны быть списком строк")
    result: list[str] = []
    for item in queries:
        value = item.strip()
        if value and value not in result:
            result.append(value)
    if not result:
        raise ValueError("LLM не предложил непустые запросы к каталогу поиска вакансий")
    return result[:4]


def build_role_choice_prompt(title: str, roles: list[ProfessionalRole]) -> list[dict[str, str]]:
    """Constrain the model to ids already observed in the live vacancy-search catalog."""
    payload = {"title": title, "roles": [asdict(role) for role in roles]}
    return [
        {
            "role": "system",
            "content": (
                "Выбери ровно одну наиболее подходящую профессию только из переданного "
                "live-каталога профессий hh.ru (фильтры поиска вакансий). Ответь только JSON: "
                '{"role_id":"...","reason":"кратко"}.'
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_role_choice(
    content: str | None, roles: list[ProfessionalRole]
) -> tuple[ProfessionalRole, str]:
    if not content or not content.strip():
        raise ValueError("LLM не выбрал профессию")
    raw = _strip_json_fence(content.strip())
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("role_id"), str):
        raise ValueError("LLM-выбор профессии должен содержать строковый role_id")
    matches = [role for role in roles if role.role_id == data["role_id"]]
    if len(matches) != 1:
        raise ValueError(
            "LLM выбрал role_id, которого нет в прочитанном live-каталоге поиска вакансий"
        )
    reason = data.get("reason")
    return matches[0], reason.strip() if isinstance(reason, str) else ""


def _role_from_tree_input(item, *, category: str) -> ProfessionalRole | None:
    tree_item = item.locator("xpath=ancestor::*[@role='treeitem'][1]")
    if tree_item.count() != 1 or tree_item.get_attribute("aria-level") != "2":
        return None
    qa = item.get_attribute("data-qa") or ""
    match = _ROLE_ID_RE.search(qa)
    if not match:
        return None
    role_id = match.group(1)
    row = item.locator("xpath=ancestor::label[1]")
    label_locator = row.locator(TREE_LABEL)
    if row.count() != 1 or label_locator.count() != 1:
        return None
    lines = [line.strip() for line in (label_locator.inner_text() or "").splitlines()]
    label = next((line for line in lines if line), "")
    if not label:
        return None
    return ProfessionalRole(role_id=role_id, label=label, category=category)


def _read_visible_roles(dialog) -> list[ProfessionalRole]:
    roles: list[ProfessionalRole] = []
    for item in dialog.locator(TREE_INPUT_ANY).all():
        role = _role_from_tree_input(item, category="")
        if role is not None:
            roles.append(role)
    return roles


def _category_from_tree_item(item) -> ProfessionalRoleCategory | None:
    if item.get_attribute("aria-level") != "1":
        return None
    category_input = item.locator("input[data-qa*='tree-selector-input-category-']")
    label_locator = item.locator(TREE_LABEL)
    if category_input.count() != 1 or label_locator.count() != 1:
        return None
    qa = category_input.get_attribute("data-qa") or ""
    match = _CATEGORY_ID_RE.search(qa)
    label = (label_locator.inner_text() or "").strip()
    if match is None or not label:
        return None
    return ProfessionalRoleCategory(match.group(1), label)


def _tree_scroll(dialog, action: str) -> bool:
    """Reset/advance the virtualized tree and return whether it is at the end."""
    return bool(
        dialog.evaluate(
            """(root, action) => {
                const nodes=[root,...root.querySelectorAll('*')];
                const n=nodes.find(e => e.scrollHeight>e.clientHeight+2 &&
                    ['auto','scroll'].includes(getComputedStyle(e).overflowY));
                if(!n) return true;
                if(action === 'reset') n.scrollTop=0;
                const atEnd=n.scrollTop+n.clientHeight>=n.scrollHeight-2;
                if(action === 'advance' && !atEnd) {
                    n.scrollTop=Math.min(n.scrollHeight,
                        n.scrollTop+Math.max(1,n.clientHeight*0.8));
                }
                return n.scrollTop+n.clientHeight>=n.scrollHeight-2;
            }""",
            action,
        )
    )


def _wait_for_tree(page: Page, dialog) -> None:
    tree_items = dialog.locator("[role='treeitem']")
    try:
        tree_items.first.wait_for(state="visible", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"дерево live-каталога поиска вакансий не отрисовалось: {exc}") from exc
    page.wait_for_timeout(100)


def _collect_categories(page: Page, dialog) -> list[ProfessionalRoleCategory]:
    _tree_scroll(dialog, "reset")
    page.wait_for_timeout(100)
    by_id: dict[str, ProfessionalRoleCategory] = {}
    previous_ids: set[str] = set()
    for _ in range(_MAX_SCROLL_STEPS):
        for item in dialog.locator("[role='treeitem'][aria-level='1']").all():
            category = _category_from_tree_item(item)
            if category is not None:
                by_id[category.category_id] = category
        current_ids = set(by_id)
        at_end = _tree_scroll(dialog, "inspect")
        if at_end and current_ids == previous_ids:
            break
        previous_ids = current_ids
        _tree_scroll(dialog, "advance")
        page.wait_for_timeout(100)
    else:
        raise RuntimeError("обход категорий live-каталога поиска вакансий не достиг конца")
    if not by_id:
        raise RuntimeError("live-каталог поиска вакансий не вернул категории профессий")
    return list(by_id.values())


def _find_category(page: Page, dialog, category_id: str):
    selector = f"[data-qa~='tree-selector-chevron-category-{category_id}']"
    _tree_scroll(dialog, "reset")
    page.wait_for_timeout(100)
    for _ in range(_MAX_SCROLL_STEPS):
        chevron = dialog.locator(selector)
        if chevron.count() == 1 and chevron.is_visible():
            return chevron
        if _tree_scroll(dialog, "inspect"):
            break
        _tree_scroll(dialog, "advance")
        page.wait_for_timeout(100)
    raise RuntimeError(
        f"категория live-каталога поиска вакансий id={category_id} потеряна при прокрутке"
    )


def _collect_category_roles(
    page: Page, dialog, category: ProfessionalRoleCategory
) -> list[ProfessionalRole]:
    chevron = _find_category(page, dialog, category.category_id)
    tree_item = chevron.locator("xpath=ancestor::*[@role='treeitem'][1]")
    if tree_item.count() != 1:
        raise RuntimeError(f"строка категории «{category.label}» неоднозначна")
    # #1004 (live IAB 2026-09-06): aria-expanded на дереве ролей обновляется
    # честно в обе стороны, но решение принимаем по ФАКТУ — отрисованным
    # leaf-строкам: атрибут мог протухнуть к моменту решения (класс #840),
    # а клик по уже раскрытой категории схлопнул бы её. Атрибут — подсказка,
    # строки — доказательство; после клика факт дожидаетсяся, иначе честный
    # отказ с состоянием атрибута.
    leaves = dialog.locator("[role='treeitem'][aria-level='2']")
    if tree_item.get_attribute("aria-expanded") != "true" or leaves.count() == 0:
        chevron.click()
        try:
            leaves.first.wait_for(state="attached", timeout=_LEAF_ATTACH_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"категория «{category.label}» не раскрылась: leaf-строки не появились "
                f"после клика по шеврону (aria-expanded="
                f"{tree_item.get_attribute('aria-expanded')!r})"
            ) from exc

    by_id: dict[str, ProfessionalRole] = {}
    seen_leaf = False
    for _ in range(_MAX_SCROLL_STEPS):
        visible_leaves = dialog.locator("[role='treeitem'][aria-level='2']")
        for leaf in visible_leaves.all():
            role_input = leaf.locator("input[data-qa*='tree-selector-input-']")
            if role_input.count() != 1:
                continue
            role = _role_from_tree_input(role_input, category=category.label)
            if role is not None:
                by_id[role.role_id] = role
                seen_leaf = True

        # Only one category is expanded at a time. Once its leaves have been
        # observed, the first viewport without any level-2 rows is past it.
        if seen_leaf and visible_leaves.count() == 0:
            break
        if _tree_scroll(dialog, "inspect"):
            break
        _tree_scroll(dialog, "advance")
        page.wait_for_timeout(100)
    else:
        raise RuntimeError(f"обход профессий категории «{category.label}» не достиг конца")

    # Collapse — bookkeeping после сбора: факт состояния дерева — aria-expanded
    # (клик щёлкает его честно, замер 2026-09-06), а строки виртуализатор может
    # НЕ размонтировать — см. _collapse_category.
    try:
        _collapse_category(page, dialog, category, leaves)
    except TreeVirtualizationWedge as exc:
        raise TreeVirtualizationWedge(str(exc), list(by_id.values())) from exc
    if not by_id:
        raise RuntimeError(f"категория «{category.label}» не вернула leaf-профессии")
    return list(by_id.values())


class TreeVirtualizationWedge(RuntimeError):
    """Дерево каталога заклинило: состояние щёлкает, список не перерисовывается.

    Живой дрилл 2026-09-06 («Маркетинг, реклама, PR»): клик по шеврону честно
    переводит категорию в aria-expanded=false, но виртуализатор hh.ru не
    размонтирует leaf-строки; следующие раскрытия только ДОБАВЛЯЮТ строки.
    Единственное известное лечение — переоткрыть диалог. Носит уже собранные
    роли категории, чтобы переоткрытие не теряло данные.
    """

    def __init__(self, message: str, roles: list[ProfessionalRole]):
        super().__init__(message)
        self.roles = roles


def _collapse_category(page: Page, dialog, category: ProfessionalRoleCategory, leaves) -> None:
    """Свернуть категорию и проверить факт по aria-expanded (#1004).

    Строки после клика — не доказательство: при клине виртуализатора они
    остаются «призраками» (см. TreeVirtualizationWedge). aria-expanded при
    этом состояние щёлкает честно в обоих направлениях (замер IAB 2026-09-06).
    """
    tree_item = None
    for _attempt in range(_COLLAPSE_ATTEMPTS):
        chevron = _find_category(page, dialog, category.category_id)
        tree_item = chevron.locator("xpath=ancestor::*[@role='treeitem'][1]")
        if tree_item.count() != 1:
            raise RuntimeError(
                f"строка категории «{category.label}» потеряна при схлопывании"
            )
        if tree_item.get_attribute("aria-expanded") != "true":
            if leaves.count() == 0:
                return
            raise TreeVirtualizationWedge(
                f"категория «{category.label}» свернута, но {leaves.count()} "
                "leaf-строк осталось в DOM — виртуализатор не размонтирует список",
                [],
            )
        chevron.click()
        page.wait_for_timeout(_COLLAPSE_POLL_MS)
    raise RuntimeError(
        f"категория «{category.label}» не сворачивается за {_COLLAPSE_ATTEMPTS} попытки: "
        f"aria-expanded={tree_item.get_attribute('aria-expanded')!r}"
    )


def _open_filters_if_needed(page: Page) -> None:
    # #858/#1004: SSR-тоггл «Фильтры» виден до того, как React привязал к нему
    # обработчики; is_visible() — не кликабельность (клик в окне гидрации
    # теряется, #840). Гейт: гидрация тоггла до первого решения по контролам.
    if not wait_for_named_control_hydration(
        page, "Фильтры", timeout_ms=_FILTERS_HYDRATION_TIMEOUT_MS
    ):
        raise RuntimeError(
            "страница поиска вакансий не гидратировалась: тоггл «Фильтры» без "
            "React-привязок — клик был бы потерян"
        )
    trigger = page.locator(FILTER_TRIGGER)
    # Desktop cycles through collapsed -> quick filters -> full filters; the
    # compact in-app layout opens the full panel in one click.
    for _ in range(2):
        if trigger.count() == 1 and trigger.is_visible():
            return
        toggles = (
            page.get_by_role("button", name="Фильтры", exact=True),
            page.get_by_role("checkbox", name="Фильтры", exact=True),
        )
        visible = [toggle for toggle in toggles if toggle.count() == 1 and toggle.is_visible()]
        if len(visible) != 1:
            raise RuntimeError(f"контрол read-only фильтров неоднозначен: {len(visible)}")
        visible[0].click()
        page.wait_for_timeout(250)


def _open_vacancy_search_catalog_dialog(page: Page):
    goto_hh(page, SEARCH_URL)
    _open_filters_if_needed(page)
    trigger = page.locator(FILTER_TRIGGER)
    try:
        trigger.first.wait_for(state="visible", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(
            f"поле professional-role в фильтрах поиска вакансий не появилось: {exc}"
        ) from exc
    if trigger.count() != 1:
        raise RuntimeError(
            f"поле professional-role в фильтрах поиска вакансий неоднозначно: {trigger.count()}"
        )
    trigger.click()

    search_anywhere = page.locator("[data-qa='tree-selector-search-input']")
    dialog = page.locator("[role='dialog']").filter(has=search_anywhere)
    try:
        dialog.first.wait_for(state="visible", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"live-каталог поиска вакансий не открылся: {exc}") from exc
    if dialog.count() != 1:
        raise RuntimeError(f"live-каталог поиска вакансий неоднозначен: {dialog.count()}")
    search = dialog.locator("[data-qa='tree-selector-search-input']")
    if search.count() != 1:
        raise RuntimeError(f"поиск live-каталога поиска вакансий неоднозначен: {search.count()}")
    return dialog, search


def collect_vacancy_search_role_catalog(page: Page) -> VacancySearchRoleCatalog:
    """Read the complete category/leaf tree without selecting or saving it."""
    dialog, search = _open_vacancy_search_catalog_dialog(page)
    search.fill("")
    _wait_for_tree(page, dialog)
    categories = _collect_categories(page, dialog)
    seen_by_id: dict[str, ProfessionalRole] = {}
    for category in categories:
        try:
            roles = _collect_category_roles(page, dialog, category)
        except TreeVirtualizationWedge as exc:
            # #1004: клин виртуализатора лечится только переоткрытием диалога.
            # Роли заклинившей категории уже собраны (исключение их несёт).
            logger.warning(
                "каталог ролей: после «%s» дерево заклинило (%s) — переоткрываю "
                "диалог, собранные роли сохранены",
                category.label,
                exc,
            )
            roles = exc.roles
            dialog, search = _open_vacancy_search_catalog_dialog(page)
            search.fill("")
            _wait_for_tree(page, dialog)
        for role in roles:
            previous = seen_by_id.get(role.role_id)
            if previous is not None:
                if normalize(previous.label) != normalize(role.label):
                    raise RuntimeError(
                        f"role_id={role.role_id} имеет разные названия «{previous.label}» "
                        f"и «{role.label}» в live-каталоге поиска вакансий"
                    )
                merged_categories = tuple(dict.fromkeys((*previous.categories, role.category)))
                seen_by_id[role.role_id] = ProfessionalRole(
                    role.role_id,
                    previous.label,
                    previous.category,
                    merged_categories,
                )
                continue
            seen_by_id[role.role_id] = ProfessionalRole(
                role.role_id, role.label, role.category, (role.category,)
            )
    return VacancySearchRoleCatalog(
        fetched_at=datetime.now(UTC),
        categories=tuple(category.label for category in categories),
        roles=tuple(seen_by_id.values()),
    )


def validate_vacancy_search_role_catalog(
    catalog: VacancySearchRoleCatalog,
) -> VacancySearchRoleCatalog:
    if catalog.fetched_at.tzinfo is None:
        raise ProfessionalRoleCacheError("fetched_at кэша должен содержать часовой пояс")
    if not catalog.categories:
        raise ProfessionalRoleCacheError("кэш каталога поиска вакансий не содержит категорий")
    if len(set(catalog.categories)) != len(catalog.categories):
        raise ProfessionalRoleCacheError(
            "кэш каталога поиска вакансий содержит повторяющиеся категории"
        )
    if any(not category.strip() for category in catalog.categories):
        raise ProfessionalRoleCacheError("кэш каталога поиска вакансий содержит пустую категорию")
    category_set = set(catalog.categories)
    if not catalog.roles:
        raise ProfessionalRoleCacheError("кэш каталога поиска вакансий не содержит профессий")
    role_ids: set[str] = set()
    categories_with_roles: set[str] = set()
    for role in catalog.roles:
        if not role.role_id.strip() or not role.label.strip():
            raise ProfessionalRoleCacheError(
                "кэш каталога поиска вакансий содержит пустой id или название"
            )
        if role.role_id in role_ids:
            raise ProfessionalRoleCacheError(
                f"кэш каталога поиска вакансий содержит повторяющийся role_id={role.role_id}"
            )
        role_categories = role.categories or ((role.category,) if role.category else ())
        if not role_categories or any(category not in category_set for category in role_categories):
            raise ProfessionalRoleCacheError(
                f"профессия role_id={role.role_id} ссылается на неизвестную категорию"
            )
        if role.category and role.category != role_categories[0]:
            raise ProfessionalRoleCacheError(
                f"основная категория role_id={role.role_id} не совпадает с categories[0]"
            )
        role_ids.add(role.role_id)
        categories_with_roles.update(role_categories)
    missing = [category for category in catalog.categories if category not in categories_with_roles]
    if missing:
        raise ProfessionalRoleCacheError(
            "кэш каталога поиска вакансий не содержит профессий для категорий: "
            + ", ".join(missing)
        )
    return catalog


def _vacancy_search_catalog_payload(catalog: VacancySearchRoleCatalog) -> dict[str, object]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source": CACHE_SOURCE,
        "locale": CACHE_LOCALE,
        "fetched_at": catalog.fetched_at.astimezone(UTC).isoformat(),
        "categories": list(catalog.categories),
        "roles": [asdict(role) for role in catalog.roles],
    }


def write_professional_role_cache(
    catalog: VacancySearchRoleCatalog, path: Path = DEFAULT_CACHE_PATH
) -> None:
    validate_vacancy_search_role_catalog(catalog)
    payload = _vacancy_search_catalog_payload(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def load_professional_role_cache(path: Path = DEFAULT_CACHE_PATH) -> VacancySearchRoleCatalog:
    if not path.is_file():
        raise ProfessionalRoleCacheError(
            f"кэш каталога поиска вакансий не найден: {path}. "
            "Выполните: hhru professional-roles --refresh"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfessionalRoleCacheError(f"кэш каталога поиска вакансий повреждён: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfessionalRoleCacheError(
            "кэш каталога поиска вакансий должен содержать JSON-объект"
        )
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ProfessionalRoleCacheError(
            "версия кэша каталога поиска вакансий несовместима; "
            "выполните: hhru professional-roles --refresh"
        )
    if payload.get("source") != CACHE_SOURCE or payload.get("locale") != CACHE_LOCALE:
        raise ProfessionalRoleCacheError(
            "source/locale кэша каталога поиска вакансий не совпадают с hh.ru/ru"
        )
    try:
        fetched_at = datetime.fromisoformat(str(payload["fetched_at"]))
        raw_categories = payload["categories"]
        raw_roles = payload["roles"]
        if not isinstance(raw_categories, list) or not all(
            isinstance(item, str) for item in raw_categories
        ):
            raise TypeError("categories")
        if not isinstance(raw_roles, list):
            raise TypeError("roles")
        parsed_roles: list[ProfessionalRole] = []
        for item in raw_roles:
            if not isinstance(item, dict):
                raise TypeError("roles")
            role_id = item["role_id"]
            label = item["label"]
            category = item["category"]
            raw_role_categories = item.get("categories", [])
            if (
                not isinstance(role_id, str)
                or not isinstance(label, str)
                or not isinstance(category, str)
                or not isinstance(raw_role_categories, list)
                or not all(isinstance(value, str) for value in raw_role_categories)
            ):
                raise TypeError("roles")
            parsed_roles.append(
                ProfessionalRole(
                    role_id=role_id,
                    label=label,
                    category=category,
                    categories=tuple(raw_role_categories),
                )
            )
        roles = tuple(parsed_roles)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfessionalRoleCacheError(
            f"структура кэша каталога поиска вакансий повреждена: {exc}"
        ) from exc
    return validate_vacancy_search_role_catalog(
        VacancySearchRoleCatalog(
            fetched_at=fetched_at,
            categories=tuple(raw_categories),
            roles=roles,
        )
    )


def professional_role_cache_is_stale(
    catalog: VacancySearchRoleCatalog,
    *,
    now: datetime | None = None,
    max_age: timedelta = CACHE_MAX_AGE,
) -> bool:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    return current - catalog.fetched_at.astimezone(UTC) > max_age


def search_cached_professional_roles(
    catalog: VacancySearchRoleCatalog, queries: list[str], *, limit: int = 20
) -> list[ProfessionalRole]:
    cleaned = [normalize(query) for query in queries if query and normalize(query)]
    if not cleaned:
        raise ValueError("для поиска по каталогу нужна непустая --query")
    if limit < 1:
        raise ValueError("--limit должен быть положительным")

    ranked: list[tuple[int, int, ProfessionalRole]] = []
    for index, role in enumerate(catalog.roles):
        label = normalize(role.label)
        scores: list[int] = []
        for query in cleaned:
            if role.role_id == query or label == query:
                scores.append(0)
            elif label.startswith(query):
                scores.append(1)
            elif query in label:
                scores.append(2)
        if scores:
            ranked.append((min(scores), index, role))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [role for _, _, role in ranked[:limit]]


def search_professional_roles(page: Page, queries: list[str]) -> list[ProfessionalRole]:
    """Return live catalog leaves matching the supplied read-only searches."""
    cleaned = [query.strip() for query in queries if query and query.strip()]
    if not cleaned:
        raise ValueError("для поиска по каталогу нужна непустая строка")

    dialog, search = _open_vacancy_search_catalog_dialog(page)

    by_id: dict[str, ProfessionalRole] = {}
    for query in cleaned:
        search.fill(query)
        # React replaces the filtered rows after the input value changes; an
        # immediate locator read can still observe leaves from the prior query.
        page.wait_for_timeout(250)
        # The tree is virtualized and can retain the previous query's scroll
        # position. Start every query from the first viewport so later queries
        # cannot silently miss roles near the top of their result set.
        _tree_scroll(dialog, "reset")
        tree_items = dialog.locator(TREE_INPUT_ANY)
        try:
            tree_items.first.wait_for(state="visible", timeout=_WAIT_MS)
        except PlaywrightTimeoutError:
            continue
        # A focused query yields a bounded filtered tree.  Scroll it to the end
        # because Magritte virtualizes longer result sets.
        previous_ids: set[str] = set()
        for _ in range(40):
            for role in _read_visible_roles(dialog):
                by_id[role.role_id] = role
            current_ids = set(by_id)
            at_end = _tree_scroll(dialog, "inspect")
            if at_end and current_ids == previous_ids:
                break
            previous_ids = current_ids
            _tree_scroll(dialog, "advance")
            page.wait_for_timeout(100)

    # Deliberately leave the modal unsubmitted.  Dry-run closes the browser;
    # the write path performs an identity-bound resume navigation next.
    return list(by_id.values())


def resolve_explicit_role(page: Page, label: str) -> ProfessionalRole:
    from .create_resume import OTHER_ROLE_LABEL

    roles = search_professional_roles(page, [label])
    matches = [role for role in roles if normalize(role.label) == normalize(label)]
    if len(matches) != 1:
        # #950: отказ ведёт к цели — перечисляем, что фильтр реально предложил
        # по запросу (принцип #836), чтобы перезапуск был точным с первого раза.
        # «Другое» предложением не является: точный save по плейсхолдеру —
        # отказ по дизайну (#913), а не повторная цель.
        offered = list(
            dict.fromkeys(
                role.label
                for role in roles
                if role.label != OTHER_ROLE_LABEL and normalize(role.label) != normalize(label)
            )
        )
        detail = f"; фильтр предлагает: {'; '.join(offered)}" if offered else ""
        raise RuntimeError(
            f"профессия «{label}» не найдена однозначно в live-каталоге поиска вакансий "
            f"(совпадений: {len(matches)}){detail}"
        )
    return matches[0]


def suggest_role(page: Page, llm, title: str) -> tuple[ProfessionalRole, str, list[str]]:
    query_response = llm.chat(build_role_query_prompt(title))
    queries = parse_role_queries(query_response.content)
    roles = search_professional_roles(page, queries)
    if not roles:
        raise RuntimeError(
            "live-каталог поиска вакансий не вернул leaf-профессии по предложенным запросам"
        )
    choice_response = llm.chat(build_role_choice_prompt(title, roles))
    role, reason = parse_role_choice(choice_response.content, roles)
    return role, reason, queries
