"""Назначение уже загруженного фото на резюме: клик по edit-button ->
модалка «Все загруженные фото» -> photo-viewer-action-assign-current.

Один мутирующий клик (assign). Остальное — навигация и чтение DOM.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hhru_bot.browser import (
    dismiss_cookie_banner,
    goto_hh,
    launch_context,
    require_authenticated_page,
)
from hhru_bot.config import load_config_or_exit
from hhru_bot.logging_setup import LOG_DIR
from hhru_bot.selector_groups.resume_photo import (
    RESUME_AVATAR_BLOCK,
    RESUME_AVATAR_IMAGE,
)

RESUME_ID = sys.argv[1]

config = load_config_or_exit("data/config.yaml")


def wait_react_props(page, selector, timeout_s=15):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        wired = page.evaluate(
            """(sel) => {
              const i = document.querySelector(sel);
              return i ? Object.keys(i).some(k => k.startsWith("__reactProps$")) : false;
            }""",
            selector,
        )
        if wired:
            return True
        page.wait_for_timeout(500)
    return False


with launch_context(config.storage_state_file, headless=True) as context:
    page = context.new_page()
    goto_hh(page, f"https://hh.ru/resume/{RESUME_ID}")
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    avatar.wait_for(state="visible", timeout=15000)
    print("img до:", page.locator(RESUME_AVATAR_IMAGE).count())

    page.evaluate(
        """() => {
          const c = document.querySelector("[class*='ContainerForMicroFrontend-resumePhotoViewer']");
          if (c) c.scrollIntoView({block: "center"});
        }"""
    )
    print(
        "гидратация:", wait_react_props(page, "input[data-qa='resume-photo-proxy-gallery-input']")
    )
    page.wait_for_timeout(1000)

    page.locator("[data-qa='resume-avatar-edit-button']").first.click()
    print("edit-button кликнут, жду модалку вьювера...")

    assign_btn = page.locator("[data-qa='photo-viewer-action-assign-current']")
    try:
        assign_btn.first.wait_for(state="visible", timeout=15000)
        print("вьювер открыт, assign-current видим")
    except Exception:
        out = LOG_DIR / f"photo_assign_nomodal_{time.strftime('%H%M%S')}.html"
        out.write_text(page.content(), encoding="utf-8")
        print("МОДАЛКА НЕ ОТКРЫЛАСЬ, дамп:", out.name)
        sys.exit(1)
    # дать анимации модалки закончиться (overlay перехватывал клики)
    page.wait_for_timeout(2500)
    out = LOG_DIR / f"photo_assign_modal_{time.strftime('%H%M%S')}.html"
    out.write_text(page.content(), encoding="utf-8")
    print("дамп модалки:", out.name)
    assign_btn.first.click(timeout=20000)
    print("assign-current кликнут")

    for _ in range(20):
        if page.locator(RESUME_AVATAR_IMAGE).count() > 0:
            break
        page.wait_for_timeout(1000)
    print("img в аватаре после assign:", page.locator(RESUME_AVATAR_IMAGE).count())
    out = LOG_DIR / f"photo_assign_final_{time.strftime('%H%M%S')}.html"
    out.write_text(page.content(), encoding="utf-8")
    print("финальный дамп:", out.name)
