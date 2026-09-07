"""Диагностика + назначение фото из библиотеки: карандашик -> вьювер ->
«Установить для этого резюме» (поток владельца, скриншоты 2026-09-03).

Один мутирующий клик (assign). Перед ним — read-only инвентарь overlay'ей
и геометрии кнопки, чтобы понять, кто перехватывает pointer events
(боевой прогон команды дважды упал на этом шаге).
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
    RESUME_AVATAR_EDIT_BUTTON,
    RESUME_AVATAR_IMAGE,
    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
)

RESUME_ID = sys.argv[1]

config = load_config_or_exit("data/config.yaml")

with launch_context(config.storage_state_file, headless=False) as context:
    page = context.new_page()
    goto_hh(page, f"https://hh.ru/resume/{RESUME_ID}")
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    avatar.wait_for(state="visible", timeout=30000)
    print("img до:", page.locator(RESUME_AVATAR_IMAGE).count())

    # Карандашик открывает вьювер только УЖЕ гидратированного микрофроненда
    page.evaluate(
        """(sel) => {
          const c = document.querySelector(sel);
          if (c) c.scrollIntoView({block: "center"});
        }""",
        "[class*='ContainerForMicroFrontend-resumePhotoViewer']",
    )
    from hhru_bot.browser import wait_for_react_hydration

    wired = wait_for_react_hydration(
        page,
        "input[data-qa='resume-photo-proxy-gallery-input']",
        timeout_ms=20000,
    )
    print("гидратация:", wired)
    page.wait_for_timeout(1000)

    pencil = page.locator(RESUME_AVATAR_EDIT_BUTTON).first
    pencil.wait_for(state="visible", timeout=15000)
    pencil.click()
    print("карандашик кликнут")

    assign_btn = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_CURRENT).first
    try:
        assign_btn.wait_for(state="visible", timeout=15000)
    except Exception:
        out = LOG_DIR / f"photo_pencil_nomodal_{time.strftime('%H%M%S')}.html"
        out.write_text(page.content(), encoding="utf-8")
        print("assign не появился, дамп:", out.name)
        raise
    page.wait_for_timeout(2500)

    # Read-only инвентарь: сколько overlay, их геометрия и что сверху в точке кнопки
    info = page.evaluate(
        """(sel) => {
          const btn = document.querySelector(sel);
          const r = btn ? btn.getBoundingClientRect() : null;
          const overlays = [...document.querySelectorAll("[data-qa='modal-overlay']")].map(o => {
            const b = o.getBoundingClientRect();
            const st = getComputedStyle(o);
            return {z: st.zIndex, display: st.display, pointer: st.pointerEvents,
                    rect: [b.x, b.y, b.width, b.height].map(Math.round)};
          });
          let topAtBtn = null;
          if (r) {
            const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            topAtBtn = el ? (el.tagName + "|" + (el.getAttribute("data-qa") || "") + "|" + el.className.toString().slice(0, 80)) : null;
          }
          return {btnRect: r ? [r.x, r.y, r.width, r.height].map(Math.round) : null,
                  viewport: [innerWidth, innerHeight], overlays, topAtBtn};
        }""",
        RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    )
    print("геометрия/overlay:", info)

    assign_btn.click(timeout=20000)
    print("assign кликнут")

    for _ in range(30):
        if page.locator(RESUME_AVATAR_IMAGE).count() > 0:
            break
        page.wait_for_timeout(1000)
    print("img после assign:", page.locator(RESUME_AVATAR_IMAGE).count())
    page.wait_for_timeout(5000)
    out = LOG_DIR / f"photo_pencil_assign_{time.strftime('%H%M%S')}.html"
    out.write_text(page.content(), encoding="utf-8")
    print("дамп:", out.name)
