"""Read-only диагностика: что рождает клик по edit-button / аватару —
попап, chooser, ничего? Дампы DOM до/после, diff по ключевым маркерам.
Мутаций hh.ru нет: только клики по UI и наблюдение.
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
from hhru_bot.selector_groups.resume_photo import RESUME_AVATAR_BLOCK

RESUME_ID = sys.argv[1]

config = load_config_or_exit("data/config.yaml")


class _R:
    resume_url = f"https://hh.ru/resume/{RESUME_ID}"


resume = _R()

MARKERS = [
    "modal-overlay",
    "role=dialog",
    "drop-base",
    "popup",
    "upload",
    "crop",
    "gallery-input",
    "fileinput",
    "FileInput",
    "Portfolio",
    "portfolio",
    "Загрузит",
    "загрузит",
    "Выберит",
    "выберит",
]


def scan(page, label):
    html = page.content()
    found = {m: html.count(m) for m in MARKERS if html.count(m)}
    print(f"[{label}] маркеры: {found}")
    return html


with launch_context(config.storage_state_file, headless=True) as context:
    page = context.new_page()
    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    page.locator(RESUME_AVATAR_BLOCK).first.wait_for(state="visible", timeout=15000)
    time.sleep(5)  # дать гидратации закончиться

    before = scan(page, "до клика")

    btn = page.locator("[data-qa='resume-avatar-edit-button']").first
    btn.click()
    time.sleep(3)
    after_btn = scan(page, "после клика edit-button")

    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    avatar.click()
    time.sleep(3)
    after_avatar = scan(page, "после клика avatar")

    diff_keys = {
        k
        for k in set(after_btn) | set(after_avatar)
        if after_btn.get(k, 0) != before.get(k, 0) or after_avatar.get(k, 0) != before.get(k, 0)
    }
    print("изменившиеся маркеры:", diff_keys or "нет")

    html = page.content()
    out = LOG_DIR / f"photo_explore_click_{time.strftime('%Y%m%d_%H%M%S')}.html"
    out.write_text(html, encoding="utf-8")
    print(f"дамп: {out}")
