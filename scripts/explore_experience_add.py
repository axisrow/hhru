#!/usr/bin/env python3
"""Read-only exploration for #786/#787: inspect form after clicking Добавить."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright

from hhru_bot.browser import goto_hh, require_authenticated_page
from hhru_bot.config import load_config_or_exit

CONFIG_PATH = "/Users/axisrow/Projects/hhru/data/config.yaml"
TARGET_SLUG = "marketing"


def main() -> int:
    config = load_config_or_exit(CONFIG_PATH)
    resume = next((r for r in config.resumes if r.id == TARGET_SLUG), None)
    if resume is None:
        print(f"resume {TARGET_SLUG!r} not found")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=str(ROOT / "data" / "storage_state" / "hh_session.json"),
            user_agent=config.user_agent,
        )
        page = context.new_page()

        print("=== open resume page ===")
        goto_hh(page, f"https://hh.ru/resume/{resume.resume_id}")
        require_authenticated_page(page)
        print(f"URL: {page.url}")

        # Click the "Добавить" link in experience card
        add_links = page.locator('[data-qa="resume-list-card-experience"] [data-qa="link"]')
        add_link = None
        for i in range(add_links.count()):
            link = add_links.nth(i)
            if "Добавить" in link.inner_text():
                add_link = link
                break

        if add_link is None:
            print("Добавить link not found")
            context.close()
            browser.close()
            return 1

        print("clicking Добавить...")
        add_link.click()
        page.wait_for_timeout(3000)
        print(f"URL after click: {page.url}")

        # Dump form HTML
        main_el = page.locator('main, [data-qa="profile-layout-form"], form')
        if main_el.count() > 0:
            html = main_el.first.inner_html()
            print(f"main HTML (first 3000 chars): {html[:3000]!r}")

        # Look for company/position inputs by various selectors
        for sel in (
            'input[placeholder*="Компания"], input[placeholder*="компания"]',
            'input[placeholder*="Должность"], input[placeholder*="должность"]',
            '[data-qa*="company"] input, [data-qa*="position"] input',
            '[data-qa*="experience"] input',
            'input[name*="company"], input[name*="position"]',
            'input[name*="employer"], input[name*="job"]',
            '[data-qa="resume-profile-experience-specific-company-input-0"]',
            '[data-qa="resume-profile-experience-specific-position-input-0"]',
        ):
            loc = page.locator(sel)
            if loc.count() > 0:
                print(f"\nFOUND input selector: {sel} (count={loc.count()})")
                for i in range(min(loc.count(), 5)):
                    try:
                        inp = loc.nth(i)
                        print(f"  [{i}] visible={inp.is_visible()} value={inp.input_value()!r} placeholder={inp.get_attribute('placeholder')!r} name={inp.get_attribute('name')!r}")
                    except Exception as exc:
                        print(f"  [{i}] error: {exc}")

        # Check for resume binding panel
        for text in ("Резюме с этим местом работы", "Привязать к резюме", "resumeFrom"):
            loc = page.locator(f':has-text("{text}")')
            if loc.count() > 0:
                print(f"\ntext '{text}' count: {loc.count()}")
                for i in range(min(loc.count(), 5)):
                    try:
                        el = loc.nth(i).evaluate("""el => ({
                            tag: el.tagName,
                            data_qa: el.getAttribute('data-qa'),
                            text: el.innerText?.slice(0, 300),
                            html: el.innerHTML?.slice(0, 500),
                        })""")
                        print(f"  [{i}] {el}")
                    except Exception as exc:
                        print(f"  [{i}] error: {exc}")

        # Check checkboxes
        checkboxes = page.locator('input[type="checkbox"]')
        print(f"\ncheckbox count: {checkboxes.count()}")
        for i in range(min(checkboxes.count(), 15)):
            cb = checkboxes.nth(i)
            try:
                label = cb.evaluate('el => el.closest("label")?.innerText || el.getAttribute("aria-label") || ""')
                print(f"  cb[{i}] checked={cb.is_checked()} label={label!r}")
            except Exception as exc:
                print(f"  cb[{i}] error: {exc}")

        # Check if target resume is mentioned
        for keyword in (resume.resume_id, "marketing", "Ведущий performance-маркетолог"):
            loc = page.locator(f':has-text("{keyword}")')
            if loc.count() > 0:
                print(f"\nkeyword '{keyword}' count: {loc.count()}")
                for i in range(min(loc.count(), 3)):
                    try:
                        text = loc.nth(i).inner_text()
                        print(f"  [{i}] {text[:200]!r}")
                    except Exception:
                        pass

        # Try to find and click cancel
        cancel = page.locator('[data-qa="profile-layout-cancel-button"], button:has-text("Отмена"), button:has-text("Назад")')
        if cancel.count() > 0:
            print(f"\ncancel button found, clicking...")
            cancel.first.click()
            page.wait_for_timeout(1000)
            print(f"URL after cancel: {page.url}")

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
