"""Разовый probe #1052: один разведочный клик по кнопке «Сохранить поиск»
(vacancy-saved-search-create) на /search/vacancy основного аккаунта.

Цель — открыть попап сохранения автопоиска и снять его DOM (селекторы поля
имени/submit/чекбоксов уведомлений). Сохранение НЕ подтверждается: после
снимка попап закрывается (Escape). Разрешение оркестратора hhru-80
(2026-09-08): ровно ОДИН клик; если hh.ru сохранит мгновенно без попапа —
факт задокументировать честно (сняв /applicant/autosearch после клика).
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
from hhru_bot.selector_groups.saved_search import SEARCH_SAVE_BUTTON  # noqa: E402

SEARCH_URL = "https://hh.ru/search/vacancy?text=python&area=1"
AUTOSEARCH_URL = "https://hh.ru/applicant/autosearch"


def main() -> None:
    state = Path("/Users/axisrow/Projects/hhru/data/storage_state/hh_session.json")
    with launch_context(state) as context:
        page = context.pages[0] if context.pages else context.new_page()
        goto_hh(page, SEARCH_URL)
        page.locator(SEARCH_SAVE_BUTTON).first.wait_for(state="visible", timeout=15_000)
        print("== до клика: census строки поиска (попапа быть не должно)")
        before = [r for r in rendered_controls_census(page) if r.get("visible")]
        print(census_table(before))

        print("== ОДИН разведочный клик по кнопке «Сохранить поиск»")
        page.locator(SEARCH_SAVE_BUTTON).first.click()
        page.wait_for_timeout(3_000)

        after = [r for r in rendered_controls_census(page) if r.get("visible")]
        new_qa = {r.get("qa") for r in after} - {r.get("qa") for r in before}
        print(f"== после клика: новых видимых qa: {sorted(q for q in new_qa if q)}")
        print(census_table(after))
        dump = dump_page_html(page, "saved_search_popup_1052")
        print(f"== html-дамп: {dump}")

        popup_snapshot = [
            r
            for r in after
            if r.get("qa") and any(k in str(r.get("qa")) for k in ("saved", "modal", "autosearch"))
        ]
        print("== MACHINE_READABLE_JSON:")
        print(json.dumps(popup_snapshot, ensure_ascii=False, indent=2))

        print("== закрытие попапа: Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1_500)
        closed = [r for r in rendered_controls_census(page) if r.get("visible")]
        still = {r.get("qa") for r in closed} & new_qa
        print(f"== после Escape новых qa осталось: {sorted(q for q in still if q)}")

        print("== контроль: список автопоисков после клика (мутации быть не должно)")
        goto_hh(page, AUTOSEARCH_URL)
        page.wait_for_timeout(2_000)
        final = [r for r in rendered_controls_census(page) if r.get("visible")]
        empty = [r for r in final if r.get("qa") == "empty-favorites-saved-search-block"]
        print(f"empty-favorites-saved-search-block visible: {bool(empty)}")
        page.close()


if __name__ == "__main__":
    main()
