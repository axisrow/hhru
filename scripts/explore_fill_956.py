"""Read-only drill (#956): open the shared experience add-form on the Дворник
draft, fill company/position/months the way experience.py does, wait, then
read back what the DOM actually holds. No save is pressed."""

from playwright.sync_api import sync_playwright

STATE = "/Users/axisrow/Projects/hhru/data/storage_state/hh_session.json"
RESUME = "4c263117ff110c845a0039ed1f525447414c53"
COMPANY = "[data-qa*='resume-profile-experience-specific-company-input']"
POSITION = "[data-qa*='resume-profile-experience-specific-position-input']"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=STATE)
    page = ctx.new_page()
    page.goto(f"https://hh.ru/resume/{RESUME}", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    add = page.locator("[data-qa='resume-list-card-experience'] [data-qa='link']")
    add.click()
    page.wait_for_timeout(4000)
    print("url:", page.url)
    comp = page.locator(COMPANY)
    pos = page.locator(POSITION)
    print("company count:", comp.count(), "position count:", pos.count())
    if comp.count():
        comp.first.fill("ТСЖ «Зелёный квартал»")
        pos.first.fill("Дворник")
        for label in ("С полями сразу",):
            pass
        page.wait_for_timeout(300)
        print("t=0.3s company=", repr(comp.first.input_value()), "position=", repr(pos.first.input_value()))
        page.wait_for_timeout(2000)
        print("t=2.3s company=", repr(comp.first.input_value()), "position=", repr(pos.first.input_value()))
        page.wait_for_timeout(5000)
        print("t=7.3s company=", repr(comp.first.input_value()), "position=", repr(pos.first.input_value()))
    browser.close()
