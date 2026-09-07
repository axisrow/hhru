"""Разовый probe #1004: почему клик схлопывания категории не доходит до
дерева в CLI-браузере (в IAB тот же жест работает). Read-only: диалог
фильтров поиска, без apply, без сохранения чего-либо."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hhru_bot.browser import launch_context, goto_hh  # noqa: E402
from hhru_bot import professional_roles as pr  # noqa: E402


def main() -> None:
    with launch_context(Path("data/storage_state/hh_session.json")) as context:
        page = context.pages[0] if context.pages else context.new_page()
        goto_hh(page, pr.SEARCH_URL)
        pr._open_filters_if_needed(page)
        dialog, _search = pr._open_vacancy_search_catalog_dialog(page)
        pr._wait_for_tree(page, dialog)
        categories = pr._collect_categories(page, dialog)
        print("категорий собрано:", len(categories))
        leaves = dialog.locator("[role='treeitem'][aria-level='2']")
        for cat in categories:
            chevron = pr._find_category(page, dialog, cat.category_id)
            ti = chevron.locator("xpath=ancestor::*[@role='treeitem'][1]")
            if ti.get_attribute("aria-expanded") != "true" or leaves.count() == 0:
                chevron.click()
                leaves.first.wait_for(state="attached", timeout=5_000)
            # мини-обход: до конца видимости
            for _ in range(30):
                if pr._tree_scroll(dialog, "inspect"):
                    break
                pr._tree_scroll(dialog, "advance")
            # схлопывание с диагностикой
            ok = False
            for attempt in range(3):
                chevron = pr._find_category(page, dialog, cat.category_id)
                if leaves.count() == 0:
                    ok = True
                    break
                box = chevron.bounding_box()
                hit = (
                    page.evaluate(
                        """([x, y]) => {
                        const t = document.elementFromPoint(x, y);
                        return t ? {tag: t.tagName, qa: (t.getAttribute('data-qa')||'').slice(0,60)} : null;
                    }""",
                        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
                    )
                    if box
                    else None
                )
                chevron.click()
                for _ in range(30):
                    if leaves.count() == 0:
                        ok = True
                        break
                    page.wait_for_timeout(100)
                if ok:
                    print(f"  «{cat.label}»: сошлось с попытки {attempt + 1}, hit={hit}")
                    break
                aria_now = ti.get_attribute("aria-expanded")
                print(f"  «{cat.label}»: клик потерян, hit={hit}, aria-expanded={aria_now!r}")
            if not ok:
                print(f"  «{cat.label}»: НЕ СХЛОПНУЛАСЬ; id={cat.category_id}")
                aria_now = ti.get_attribute("aria-expanded")
                print("  aria-expanded после попыток:", aria_now)
                # призраки должно вытеснить следующее раскрытие
                ids_js = (
                    "() => [...document.querySelectorAll("
                    "\"[role='treeitem'][aria-level='2'] input\")]"
                    ".map(i => (i.getAttribute('data-qa')||'').slice(-12))"
                )
                ids_before = page.evaluate(ids_js)
                nxt = categories[categories.index(cat) + 1]
                nch = pr._find_category(page, dialog, nxt.category_id)
                nch.click()
                page.wait_for_timeout(900)
                ids_after = page.evaluate(ids_js)
                print("  id строк до раскрытия следующей:", ids_before[:12])
                print("  id строк после:", ids_after[:14])
                print("  призраки вытеснены:", set(ids_before).isdisjoint(set(ids_after)))
                return
                detail = page.evaluate(
                    """() => [...document.querySelectorAll("[role='treeitem'][aria-level='2']")].map(r => ({
                        qa: (r.getAttribute('data-qa') || '').slice(0, 40), h: r.offsetHeight,
                        top: Math.round(r.getBoundingClientRect().top)}))"""
                )
                print("  оставшиеся строки:", detail[:10])
                return
        print("ВСЕ КАТЕГОРИИ СОШЛИСЬ")


if __name__ == "__main__":
    main()
