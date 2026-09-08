"""Боевой прогон #1052 (разрешение оркестратора hhru-80, 2026-09-08, ОДИН прогон):
создание ОДНОГО автопоиска кликом «На почту» в tooltip кнопки «Сохранить поиск».

Шаги: /search/vacancy?text=python&area=1 → клик SEARCH_SAVE_BUTTON (безвреден,
разведка 2026-09-08) → wait tooltip → census → клик SEARCH_SAVE_CHANNEL_EMAIL
(мутирующая граница) → census/dump следующего экрана → readback
/applicant/autosearch (строка автопоиска + census её селекторов).

Повторные клик-действия — только через новое согласование.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hhru_bot.browser import (  # noqa: E402
    census_table,
    dump_page_html,
    goto_hh,
    launch_context,
    rendered_controls_census,
)
from hhru_bot.selector_groups.saved_search import (  # noqa: E402
    SEARCH_SAVE_BUTTON,
    SEARCH_SAVE_CHANNEL_EMAIL,
    SEARCH_SAVE_DROPDOWN,
)

SEARCH_URL = "https://hh.ru/search/vacancy?text=python&area=1"
AUTOSEARCH_URL = "https://hh.ru/applicant/autosearch"


def _visible(page) -> list[dict]:
    return [r for r in rendered_controls_census(page) if r.get("visible")]


def main() -> None:
    state = Path("/Users/axisrow/Projects/hhru/data/storage_state/hh_session.json")
    with launch_context(state) as context:
        page = context.pages[0] if context.pages else context.new_page()
        goto_hh(page, SEARCH_URL)
        page.locator(SEARCH_SAVE_BUTTON).first.wait_for(state="visible", timeout=15_000)
        before = {r.get("qa") for r in _visible(page)}

        print("== клик 1 (безвредный): кнопка «Сохранить поиск»")
        page.locator(SEARCH_SAVE_BUTTON).first.click()
        page.locator(SEARCH_SAVE_DROPDOWN).first.wait_for(state="visible", timeout=10_000)
        tooltip = [r for r in _visible(page) if r.get("qa") not in before or not r.get("qa")]
        print(census_table(tooltip))

        print("== клик 2 (МУТИРУЮЩИЙ, разрешён один): канал «На почту»")
        channel = page.locator(SEARCH_SAVE_CHANNEL_EMAIL)
        if channel.count() != 1:
            print(f"[ABORT] кнопка канала неоднозначна/не найдена: {channel.count()}")
            dump_page_html(page, "saved_search_channel_abort_1052")
            return
        channel.first.click()
        page.wait_for_timeout(5_000)

        after = _visible(page)
        new_qa = sorted(
            str(q) for q in ({r.get("qa") for r in after} - before) if q and q != "None"
        )
        print(f"== после клика канала: новых видимых qa: {new_qa}")
        print(census_table(after))
        dump = dump_page_html(page, "saved_search_channel_email_1052")
        print(f"== html-дамп: {dump}")
        snapshot = [r for r in after if r.get("qa") and "saved" in str(r.get("qa"))] + [
            r for r in after if r.get("qa") in new_qa
        ]
        print("== MACHINE_READABLE_JSON (новые/ saved-контролы):")
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))

        print("== readback: /applicant/autosearch (строка автопоиска)")
        goto_hh(page, AUTOSEARCH_URL)
        page.wait_for_timeout(3_000)
        final = _visible(page)
        empty = [r for r in final if r.get("qa") == "empty-favorites-saved-search-block"]
        print(f"empty-favorites-saved-search-block visible: {bool(empty)}")
        print(census_table(final))
        dump2 = dump_page_html(page, "saved_search_autosearch_readback_1052")
        print(f"== html-дамп readback: {dump2}")
        page.close()


if __name__ == "__main__":
    main()
