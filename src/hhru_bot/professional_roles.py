"""Read and resolve hh.ru professional-role catalog entries through live UI.

The vacancy-search filter is deliberately used as the read-only catalog
surface.  It exposes the same role ids as the resume wizard without requiring
the wizard's first ``Save and continue`` click, so dry-run can validate a
classification before any resume mutation (#574).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh
from .external_forms.detect import normalize

SEARCH_URL = f"{HH_BASE_URL}/search/vacancy"
FILTER_TRIGGER = "[data-qa='search-filter-professional-role-trigger']"
TREE_INPUT = "[data-qa~='tree-selector-input-{}']"
TREE_INPUT_ANY = "input[data-qa*='tree-selector-input-']"
TREE_LABEL = "[data-qa='cell-text-content']"
_ROLE_ID_RE = re.compile(r"(?:^|\s)tree-selector-input-(\d+)(?:\s|$)")
_WAIT_MS = 15_000


@dataclass(frozen=True)
class ProfessionalRole:
    role_id: str
    label: str
    category: str = ""


def build_role_query_prompt(title: str) -> list[dict[str, str]]:
    """Ask for a few Russian catalog-search phrases, not a classification."""
    return [
        {
            "role": "system",
            "content": (
                "Подбери 1-4 коротких русских поисковых запроса для live-каталога "
                "профессий hh.ru. Не выбирай профессию и не выдумывай каталог. "
                'Ответь только JSON: {"queries":["..."]}.'
            ),
        },
        {"role": "user", "content": title},
    ]


def parse_role_queries(content: str | None) -> list[str]:
    if not content or not content.strip():
        raise ValueError("LLM не предложил запросы к каталогу профессий")
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    data = json.loads(raw)
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or not all(isinstance(item, str) for item in queries):
        raise ValueError("LLM-запросы к каталогу должны быть списком строк")
    result: list[str] = []
    for item in queries:
        value = item.strip()
        if value and value not in result:
            result.append(value)
    if not result:
        raise ValueError("LLM не предложил непустые запросы к каталогу профессий")
    return result[:4]


def build_role_choice_prompt(title: str, roles: list[ProfessionalRole]) -> list[dict[str, str]]:
    """Constrain the model to ids already observed in the live catalog."""
    payload = {"title": title, "roles": [asdict(role) for role in roles]}
    return [
        {
            "role": "system",
            "content": (
                "Выбери ровно одну наиболее подходящую профессию только из переданного "
                "live-каталога hh.ru. Ответь только JSON: "
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
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("role_id"), str):
        raise ValueError("LLM-выбор профессии должен содержать строковый role_id")
    matches = [role for role in roles if role.role_id == data["role_id"]]
    if len(matches) != 1:
        raise ValueError("LLM выбрал role_id, которого нет в прочитанном live-каталоге")
    reason = data.get("reason")
    return matches[0], reason.strip() if isinstance(reason, str) else ""


def _role_from_tree_input(item, *, category: str) -> ProfessionalRole | None:
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


def _open_filters_if_needed(page: Page) -> None:
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


def search_professional_roles(page: Page, queries: list[str]) -> list[ProfessionalRole]:
    """Return live catalog leaves matching the supplied read-only searches."""
    cleaned = [query.strip() for query in queries if query and query.strip()]
    if not cleaned:
        raise ValueError("для поиска по каталогу нужна непустая строка")

    goto_hh(page, SEARCH_URL)
    _open_filters_if_needed(page)
    trigger = page.locator(FILTER_TRIGGER)
    try:
        trigger.first.wait_for(state="visible", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"поле live-каталога профессий не появилось: {exc}") from exc
    if trigger.count() != 1:
        raise RuntimeError(f"поле live-каталога профессий неоднозначно: {trigger.count()}")
    trigger.click()

    search_anywhere = page.locator("[data-qa='tree-selector-search-input']")
    dialog = page.locator("[role='dialog']").filter(has=search_anywhere)
    try:
        dialog.first.wait_for(state="visible", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"live-каталог профессий не открылся: {exc}") from exc
    if dialog.count() != 1:
        raise RuntimeError(f"live-каталог профессий неоднозначен: {dialog.count()}")
    search = dialog.locator("[data-qa='tree-selector-search-input']")
    if search.count() != 1:
        raise RuntimeError(f"поиск live-каталога неоднозначен: {search.count()}")

    by_id: dict[str, ProfessionalRole] = {}
    for query in cleaned:
        search.fill(query)
        # React replaces the filtered rows after the input value changes; an
        # immediate locator read can still observe leaves from the prior query.
        page.wait_for_timeout(250)
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
            at_end = dialog.evaluate(
                """root => { const nodes=[root,...root.querySelectorAll('*')];
                const n=nodes.find(e => e.scrollHeight>e.clientHeight+2 &&
                    ['auto','scroll'].includes(getComputedStyle(e).overflowY));
                if(!n) return true; const end=n.scrollTop+n.clientHeight>=n.scrollHeight-2;
                if(!end) n.scrollTop=Math.min(n.scrollHeight,n.scrollTop+n.clientHeight*0.8);
                return end; }"""
            )
            if at_end and current_ids == previous_ids:
                break
            previous_ids = current_ids
            page.wait_for_timeout(100)

    # Deliberately leave the modal unsubmitted.  Dry-run closes the browser;
    # the write path performs an identity-bound resume navigation next.
    return list(by_id.values())


def resolve_explicit_role(page: Page, label: str) -> ProfessionalRole:
    roles = search_professional_roles(page, [label])
    matches = [role for role in roles if normalize(role.label) == normalize(label)]
    if len(matches) != 1:
        raise RuntimeError(
            f"профессия «{label}» не найдена однозначно в live-каталоге "
            f"(совпадений: {len(matches)})"
        )
    return matches[0]


def suggest_role(page: Page, llm, title: str) -> tuple[ProfessionalRole, str, list[str]]:
    query_response = llm.chat(build_role_query_prompt(title))
    queries = parse_role_queries(query_response.content)
    roles = search_professional_roles(page, queries)
    if not roles:
        raise RuntimeError("live-каталог не вернул leaf-профессии по предложенным запросам")
    choice_response = llm.chat(build_role_choice_prompt(title, roles))
    role, reason = parse_role_choice(choice_response.content, roles)
    return role, reason, queries
