"""Живой прогон полного потока загрузки фото: гидратация инпута ->
set_input_files -> crop-редактор (photo-editor-apply) -> модалка назначения
(photo-viewer-action-assign-current) -> маркер успеха.

Клики — боевые мутирующие действия (это и есть санкционированная загрузка
фото). Дампы DOM на каждом шаге — в data/logs/.
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
    RESUME_PHOTO_FILE_INPUT,
)

RESUME_ID = sys.argv[1]
PHOTO = sys.argv[2]

config = load_config_or_exit("data/config.yaml")


def dump(page, label):
    out = LOG_DIR / f"photo_flow_{label}_{time.strftime('%H%M%S')}.html"
    out.write_text(page.content(), encoding="utf-8")
    print(f"  дамп: {out.name}")


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
    page.locator(RESUME_AVATAR_BLOCK).first.wait_for(state="visible", timeout=15000)
    print("1. аватар отрисован, img:", page.locator(RESUME_AVATAR_IMAGE).count())

    # Гидратация микрофроненда: скролл контейнера в вьюпорт + поллинг reactProps
    page.evaluate(
        """() => {
          const c = document.querySelector("[class*='ContainerForMicroFrontend-resumePhotoViewer']");
          if (c) c.scrollIntoView({block: "center"});
        }"""
    )
    ok = wait_react_props(page, "input[data-qa='resume-photo-proxy-gallery-input']")
    print("2. гидратация gallery-input:", ok)
    if not ok:
        dump(page, "no_hydration")
        sys.exit(1)

    inputs = page.locator(RESUME_PHOTO_FILE_INPUT)
    print("   input count:", inputs.count())
    inputs.first.set_input_files(PHOTO)
    print("3. файл передан, жду редактор...")

    editor_apply = page.locator("[data-qa='photo-editor-apply']")
    try:
        editor_apply.first.wait_for(state="visible", timeout=15000)
        print("4. crop-редактор открыт, apply видим")
    except Exception:
        print("4. РЕДАКТОР НЕ ОТКРЫЛСЯ")
        dump(page, "no_editor")
        sys.exit(1)
    dump(page, "editor")

    editor_apply.first.click()
    print("5. apply кликнут, жду upload + модалку назначения...")

    assign_btn = page.locator("[data-qa='photo-viewer-action-assign-current']")
    try:
        assign_btn.first.wait_for(state="visible", timeout=30000)
        print("6. модалка назначения открыта, assign-current видим")
        dump(page, "assign_modal")
    except Exception:
        print("6. МОДАЛКА НАЗНАЧЕНИЯ НЕ ОТКРЫЛАСЬ (возможно, upload ещё идёт или авто-assign)")
        dump(page, "no_assign")
        # продолжаем проверять аватар — вдруг назначение прошло автоматически

    if assign_btn.count() > 0 and assign_btn.first.is_visible():
        assign_btn.first.click()
        print("7. assign-current кликнут")

    # маркер успеха: img в блоке аватара
    for _ in range(30):
        if page.locator(RESUME_AVATAR_IMAGE).count() > 0:
            break
        page.wait_for_timeout(1000)
    print("8. img в аватаре:", page.locator(RESUME_AVATAR_IMAGE).count())
    dump(page, "final")
