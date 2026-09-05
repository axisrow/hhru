"""Инструментированная диагностика активации assign-кнопки (боевой прогон).

Воспроизводит поток CLI upload-photo до модалки назначения и на шаге assign
перебирает способы активации с пост-проверками между попытками:
dispatch_event -> el.click() через evaluate -> focus+Enter. Логирует консоль
страницы, pageerror, геометрию/гидратацию кнопки. Останавливается на первом
эффекте (модалка закрылась / маркер появился). Боевые мутирующие шаги:
set_input_files, editor-apply, assign-активация.
"""

from __future__ import annotations

import os
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
    wait_for_react_hydration,
)
from hhru_bot.config import load_config_or_exit
from hhru_bot.logging_setup import LOG_DIR
from hhru_bot.resume_photo import _HYDRATION_TIMEOUT_MS, PHOTO_VIEWPORT
from hhru_bot.selector_groups.resume_photo import (
    RESUME_AVATAR_BLOCK,
    RESUME_AVATAR_IMAGE,
    RESUME_PHOTO_EDITOR_APPLY,
    RESUME_PHOTO_FILE_INPUT,
    RESUME_PHOTO_MFE_CONTAINER,
    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
)

RESUME_ID = sys.argv[1]
PHOTO = sys.argv[2] if len(sys.argv) > 2 else None
# режим pencil: открыть вьювер кликом по карандашу (без upload) — как в
# живом браузере, где assign по программному клику СРАБОТАЛ
PENCIL_MODE = PHOTO is None

config = load_config_or_exit("/Users/axisrow/Projects/hhru/data/accounts/default/config.yaml")
console_log: list[str] = []


def observe(page, label):
    state = page.evaluate(
        """() => {
            const btn = document.querySelector("[data-qa='photo-viewer-action-assign-current']");
            const modal = document.querySelector("aria-modal, [aria-modal='true']");
            const avatar = document.querySelector("[data-qa='resume-avatar']");
            let btnInfo = null;
            if (btn) {
                const r = btn.getBoundingClientRect();
                const keys = Object.keys(btn);
                btnInfo = {
                    rect: [Math.round(r.x), Math.round(r.y),
                           Math.round(r.width), Math.round(r.height)],
                    hydrated: keys.some(k => k.startsWith('__reactFiber'))
                              && keys.some(k => k.startsWith('__reactProps')),
                };
            }
            return {
                assignBtn: btnInfo,
                modalOpen: !!modal,
                markerImg: !!(avatar && avatar.querySelector('img')),
                hasPhotoState: document.body.innerHTML.includes('"hasPhoto":true'),
            };
        }"""
    )
    print(f"  [{label}] {state}")
    return state


def main():
    with launch_context(
        config.storage_state_file,
        headless=os.environ.get("HEADED") != "1",
        user_agent=config.user_agent,
        viewport=PHOTO_VIEWPORT,
    ) as context:
        page = context.new_page()
        page.on(
            "console",
            lambda msg: console_log.append(f"{msg.type}: {msg.text[:200]}"),
        )
        page.on("pageerror", lambda exc: console_log.append(f"PAGEERROR: {str(exc)[:300]}"))
        net_log: list[str] = []

        def _on_response(resp):
            url = resp.url
            if any(k in url for k in ("photo", "assign", "gallery", "avatar")):
                net_log.append(f"{resp.status} {resp.request.method} {url[:140]}")

        page.on("response", _on_response)

        goto_hh(page, f"https://hh.ru/resume/{RESUME_ID}")
        require_authenticated_page(page)
        dismiss_cookie_banner(page)
        avatar = page.locator(RESUME_AVATAR_BLOCK)
        avatar.wait_for(state="visible", timeout=15_000)
        if page.locator(RESUME_AVATAR_IMAGE).count() > 0 and not PENCIL_MODE:
            # для pencil-режима повторный assign того же фото безвреден,
            # гард только для upload-маршрута (боевая замена не поддерживается)
            print("[ABORT] на резюме уже есть фото — боевая замена не поддерживается")
            return
        if PENCIL_MODE:
            # карандаш открывает вьювер только УЖЕ гидратированного MFE
            # (см. selector_groups/resume_photo.py) — гидратация обязательна
            container = page.locator(RESUME_PHOTO_MFE_CONTAINER)
            page.evaluate("el => el.scrollIntoView({block: 'center'})", container.element_handle())
            if not wait_for_react_hydration(
                page, RESUME_PHOTO_FILE_INPUT, timeout_ms=_HYDRATION_TIMEOUT_MS
            ):
                print("[FAIL] инпут не гидратировался (pencil)")
                return
            pencil = page.locator("[data-qa='resume-avatar-edit-button']").first
            pencil.wait_for(state="visible", timeout=15_000)
            pencil.click()
            print("[OK] карандаш-клик, вьювер открыт (без upload)")
            # кнопка может быть rendered, но не visible (detached NavBar) —
            # ждём attached и меряем всё как есть
            try:
                assign_btn = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_CURRENT).first
                assign_btn.wait_for(state="attached", timeout=10_000)
            except PlaywrightError:
                print("[FAIL] assign-кнопка не появилась даже attached")
                state = observe(page, "нет-кнопки")
                dump_btns = page.evaluate(
                    """() => [...document.querySelectorAll("[data-qa]")]
                        .map(e => e.getAttribute('data-qa'))
                        .filter(q => /photo|viewer|assign/i.test(q))"""
                )
                print(f"  photo-viewer-* на странице: {dump_btns}")
                (LOG_DIR / f"assign_activation_console_{time.strftime('%H%M%S')}.txt").write_text(
                    "\n".join(console_log + ["", "NET:"] + net_log) or "(пусто)",
                    encoding="utf-8",
                )
                return
        else:
            container = page.locator(RESUME_PHOTO_MFE_CONTAINER)
            page.evaluate("el => el.scrollIntoView({block: 'center'})", container.element_handle())
            if not wait_for_react_hydration(
                page, RESUME_PHOTO_FILE_INPUT, timeout_ms=_HYDRATION_TIMEOUT_MS
            ):
                print("[FAIL] инпут не гидратировался")
                return
            page.locator(RESUME_PHOTO_FILE_INPUT).first.set_input_files(PHOTO)
            print("[OK] set_input_files")
            editor = page.locator(RESUME_PHOTO_EDITOR_APPLY).first
            editor.wait_for(state="visible", timeout=15_000)
            editor.click()
            print("[OK] editor-apply")

        assign_btn = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_CURRENT).first
        if not PENCIL_MODE:
            assign_btn.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(2_500)
        print("[OK] модалка назначения открыта; состояние после settle:")
        observe(page, "settle")

        hydrated = wait_for_react_hydration(
            page, RESUME_PHOTO_VIEWER_ASSIGN_CURRENT, timeout_ms=15_000
        )
        print(f"  гидратация кнопки: {hydrated}")
        deep = page.evaluate(
            """(sel) => {
                const btns = [...document.querySelectorAll(sel)];
                const modals = document.querySelectorAll("[aria-modal='true']").length;
                return btns.map(b => {
                    const propsKey = Object.keys(b).find(k => k.startsWith('__reactProps$'));
                    const props = propsKey ? b[propsKey] : null;
                    return {
                        propsKey: !!propsKey,
                        hasOnClick: !!(props && typeof props.onClick === 'function'),
                        handlerNames: props ? Object.keys(props) : [],
                    };
                }).concat([{ ariaModalCount: modals }]);
            }""",
            RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
        )
        print(f"  deep: {deep}")

        attempts = [
            ("dispatch_event", lambda: assign_btn.dispatch_event("click")),
            (
                "el.click()",
                lambda: page.evaluate(
                    "sel => document.querySelector(sel).click()",
                    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
                ),
            ),
            (
                "props.onClick",
                lambda: page.evaluate(
                    """(sel) => {
                        const b = document.querySelector(sel);
                        const k = Object.keys(b).find(k => k.startsWith('__reactProps$'));
                        b[k].onClick(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    }""",
                    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
                ),
            ),
            (
                "focus+Enter",
                lambda: (assign_btn.focus(), page.keyboard.press("Enter")),
            ),
        ]
        for name, act in attempts:
            try:
                act()
                print(f"[ATTEMPT] {name}: отправлено без ошибки")
            except PlaywrightError as exc:
                print(f"[ATTEMPT] {name}: PlaywrightError: {str(exc)[:200]}")
            page.wait_for_timeout(3_000)
            state = observe(page, f"после {name}")
            if not state["modalOpen"] or state["markerImg"] or state["hasPhotoState"]:
                print(f"[EFFECT] активация '{name}' дала эффект!")
                break
        else:
            print("[NO EFFECT] ни один способ не сработал")

        (LOG_DIR / f"assign_activation_console_{time.strftime('%H%M%S')}.txt").write_text(
            "\n".join(console_log) or "(консоль пуста)", encoding="utf-8"
        )
        print(f"  консоль страницы: {len(console_log)} записей -> data/logs/")


main()
