#!/usr/bin/env python3
"""Read-only exploration: photo block on the resume page.

Dumps HTML to data/logs/ and prints structured findings. Read-only: if a
file chooser opens after clicking the photo control, no file is ever set —
the chooser is abandoned, which cancels it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from hhru_bot.browser import dismiss_cookie_banner, goto_hh, require_authenticated_page
from hhru_bot.config import load_config_or_exit

CONFIG_PATH = str(ROOT / "data" / "config.yaml")
LOG_DIR = ROOT / "data" / "logs"


def describe(loc, index: int) -> None:
    try:
        info = loc.nth(index).evaluate(
            """el => ({
                tag: el.tagName,
                data_qa: el.getAttribute('data-qa'),
                id: el.id,
                cls: (el.className || '').toString().slice(0, 120),
                text: (el.innerText || '').slice(0, 120),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            })"""
        )
        print(f"  [{index}] {info}")
    except Exception as exc:
        print(f"  [{index}] error: {exc}")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: explore_photo_block.py <resume_id>")
        return 1
    resume_id = sys.argv[1]
    config = load_config_or_exit(CONFIG_PATH)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=str(ROOT / "data" / "storage_state" / "hh_session.json"),
            user_agent=config.user_agent,
        )
        page = context.new_page()

        print("=== open resume page ===")
        goto_hh(page, f"https://hh.ru/resume/{resume_id}")
        require_authenticated_page(page)
        dismiss_cookie_banner(page)
        print(f"URL: {page.url}")

        full_path = LOG_DIR / f"explore_photo_full_{stamp}.html"
        full_path.write_text(page.content(), encoding="utf-8")
        print(f"full page HTML: {full_path}")

        print("\n=== input[type=file] in resting DOM ===")
        inputs = page.locator("input[type='file']")
        print(f"count: {inputs.count()}")
        for i in range(inputs.count()):
            try:
                attrs = inputs.nth(i).evaluate(
                    """el => ({
                        accept: el.getAttribute('accept'),
                        hidden: el.hidden,
                        display: getComputedStyle(el).display,
                        data_qa: el.getAttribute('data-qa'),
                        name: el.name,
                        parent_tag: el.parentElement?.tagName,
                        parent_data_qa: el.parentElement?.getAttribute('data-qa'),
                    })"""
                )
                print(f"  [{i}] {attrs}")
            except Exception as exc:
                print(f"  [{i}] error: {exc}")

        print("\n=== elements mentioning Фото ===")
        hits = page.locator(
            "[data-qa*='photo' i], [data-qa*='avatar' i], "
            "[class*='photo' i], [aria-label*='фото' i]"
        )
        print(f"count: {hits.count()}")
        for i in range(min(hits.count(), 20)):
            describe(hits, i)

        print("\n=== clickable elements with text Фото ===")
        clickables = page.locator(
            "button:has-text('фото'), a:has-text('фото'), "
            "[role='button']:has-text('фото'), span:has-text('Фото')"
        )
        print(f"count: {clickables.count()}")
        for i in range(min(clickables.count(), 20)):
            describe(clickables, i)

        # Find the add-photo control: prefer data-qa hit, else text hit.
        add_button = None
        for sel in (
            "[data-qa*='photo' i][role='button'], [data-qa*='photo' i] button, "
            "button[data-qa*='photo' i]",
            "button:has-text('Добавить фото'), button:has-text('Загрузить фото'), "
            "[role='button']:has-text('Добавить фото')",
        ):
            loc = page.locator(sel)
            if loc.count() >= 1:
                add_button = loc.first
                print(f"\nadd-photo control via: {sel} (count={loc.count()})")
                break
        if add_button is None:
            print("\nadd-photo control NOT found by any probe selector")
        else:
            print("\n=== click add-photo control (chooser intercepted, no file set) ===")
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    add_button.click()
                chooser = fc_info.value
                print("FILE CHOOSER OPENED — native chooser mechanism confirmed")
                print(f"  is_multiple: {chooser.is_multiple()}")
                # No set_files() — read-only; abandoning cancels the chooser.
            except Exception as exc:
                print(f"no native chooser within 5s: {type(exc).__name__}")
                page.wait_for_timeout(2000)
                modal_path = LOG_DIR / f"explore_photo_after_click_{stamp}.html"
                modal_path.write_text(page.content(), encoding="utf-8")
                print(f"DOM after click dumped: {modal_path}")
                print(f"URL after click: {page.url}")
                modals = page.locator("[role='dialog'], .modal, [data-qa*='modal' i]")
                print(f"dialog/modal count: {modals.count()}")
                for i in range(min(modals.count(), 5)):
                    describe(modals, i)

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
