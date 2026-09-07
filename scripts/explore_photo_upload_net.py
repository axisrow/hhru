"""Read-only диагностика загрузки фото: какие сетевые запросы рождает
set_input_files на resume-photo-proxy-gallery-input.

Не мутирует ничего кроме попытки передачи файла в file-input (тот же боевой
механизм команды upload-photo). Наблюдает request/response события страницы.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.sync_api import Error as PlaywrightError

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
    RESUME_PHOTO_FILE_INPUT,
)

RESUME_ID = sys.argv[1]
PHOTO = sys.argv[2]

config = load_config_or_exit("data/config.yaml")


class _R:
    resume_url = f"https://hh.ru/resume/{RESUME_ID}"


resume = _R()

observed: list[str] = []

with launch_context(config.storage_state_file, headless=True) as context:
    page = context.new_page()

    def on_request(req):
        if "hh.ru" in req.url and req.method != "GET":
            observed.append(f"REQ {req.method} {req.url}")
        elif any(k in req.url for k in ("upload", "photo", "avatar")):
            observed.append(f"REQ {req.method} {req.url}")

    def on_response(resp):
        if any(k in resp.url for k in ("upload", "photo", "avatar")):
            observed.append(f"RESP {resp.status} {resp.url}")

    page.on("request", on_request)
    page.on("response", on_response)

    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    avatar.wait_for(state="visible", timeout=15000)

    img_before = page.locator(RESUME_AVATAR_IMAGE).count()
    print(f"img до передачи: {img_before}")

    inp = page.locator(RESUME_PHOTO_FILE_INPUT).first
    print(f"file-input count: {page.locator(RESUME_PHOTO_FILE_INPUT).count()}")

    # Подписка на change/input события в DOM — доходит ли событие до инпута.
    page.evaluate(
        """() => {
          window.__photoEvents = [];
          const inp = document.querySelector("input[data-qa='resume-photo-proxy-gallery-input']");
          if (!inp) return "no-input";
          inp.addEventListener('change', () => window.__photoEvents.push(
            'change files=' + (inp.files ? inp.files.length : 'null') +
            ' name=' + (inp.files && inp.files[0] ? inp.files[0].name : '-')));
          inp.addEventListener('input', () => window.__photoEvents.push('input'));
          return "ok";
        }"""
    )

    inp.set_input_files(PHOTO)
    print("set_input_files выполнен")

    for i in range(20):
        time.sleep(1)
        events = page.evaluate("() => window.__photoEvents")
        if events or observed:
            break
    print(f"DOM-события на инпуте: {page.evaluate('() => window.__photoEvents')}")

    # Гипотеза: обработчик активируется кликом по edit-button (или по аватару).
    # Кликаем и смотрим, меняется ли DOM и появляются ли события/запросы.
    page.evaluate("() => { window.__photoEvents = []; }")
    btn = page.locator("[data-qa='resume-avatar-edit-button']").first
    btn.click()
    time.sleep(2)
    print("после клика по edit-button:")
    print(f"  DOM-события: {page.evaluate('() => window.__photoEvents')}")
    print(f"  новые сетевые: {observed[-5:]}")
    chooser = None
    try:
        with page.expect_file_chooser(timeout=3000) as fc_info:
            btn.click()
        chooser = fc_info.value
        print("  file chooser ОТКРЫЛСЯ на втором клике")
    except PlaywrightError:
        print("  file chooser не открылся на втором клике")
    if chooser is not None:
        chooser.set_files(PHOTO)
        time.sleep(5)
        print(
            f"  после chooser: img={page.locator(RESUME_AVATAR_IMAGE).count()}, "
            f"события={page.evaluate('() => window.__photoEvents')}"
        )
    print(f"  новые сетевые: {observed[-10:]}")
    time.sleep(5)
    img_after = page.locator(RESUME_AVATAR_IMAGE).count()
    print(f"img финально: {img_after}")
    print(
        "файлы в инпуте:",
        page.evaluate(
            "() => { const i = document.querySelector(\"input[data-qa='resume-photo-proxy-gallery-input']\");"
            " return i && i.files ? i.files.length : 'gone'; }"
        ),
    )

    html = page.content()
    out = LOG_DIR / f"photo_explore_net_{time.strftime('%Y%m%d_%H%M%S')}.html"
    out.write_text(html, encoding="utf-8")
    print(f"дамп: {out}")
