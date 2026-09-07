"""Разовый probe #1030: почему get_by_role("button", name="Фильтры") не находит
тоггл на /search/vacancy под свежим аккаунтом, хотя census его видит.
Гипотеза: aria-label="" (пустой) перекрывает innerText в accessible name.
Read-only: goto + чтение атрибутов, ни одного клика."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hhru_bot.browser import goto_hh, launch_context  # noqa: E402
from hhru_bot import professional_roles as pr  # noqa: E402

STATE = {
    "marketing": Path("data/accounts/marketing/storage_state/hh_session.json"),
    "default": Path("data/accounts/default/storage_state/hh_session.json"),
}

JS = """
() => {
  const el = document.querySelector("[data-qa='header-search-filters-button']");
  if (!el) return { found: false };
  return {
    found: true,
    outerHTML: el.outerHTML,
    hasAriaLabelAttr: el.hasAttribute("aria-label"),
    ariaLabel: el.getAttribute("aria-label"),
    ariaLabelledby: el.getAttribute("aria-labelledby"),
    innerText: (el.innerText || "").trim(),
    ariaExpanded: el.getAttribute("aria-expanded"),
    disabled: el.disabled === true,
  };
}
"""


def main() -> None:
    for name, state in STATE.items():
        if not state.exists():
            print(f"== {name}: нет storage_state, пропускаю")
            continue
        print(f"== {name} ({state})")
        with launch_context(state) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_hh(page, pr.SEARCH_URL)
            page.wait_for_timeout(3_000)
            info = page.evaluate(JS)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            page.close()


if __name__ == "__main__":
    main()
