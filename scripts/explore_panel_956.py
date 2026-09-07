"""Read-only drill (#956): open the shared experience editor for the Дворник
draft and dump the resume-binding panel: which titles/checkboxes are really
in the DOM. No save is pressed; the form is left via Cancel."""

from playwright.sync_api import sync_playwright

STATE = "data/storage_state/hh_session.json"
RESUME = "0000111122223333444455556666777788889999"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=STATE)
    page = ctx.new_page()
    page.goto(f"https://hh.ru/resume/{RESUME}", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    add = page.locator("[data-qa='resume-list-card-experience'] [data-qa='link']")
    print("add count:", add.count())
    add.click()
    page.wait_for_timeout(4000)
    print("url:", page.url)
    # find the binding panel
    panel = page.locator("text=Резюме с этим местом работы")
    print("panel text count:", panel.count())
    boxes = page.locator("[role='checkbox']")
    print("role=checkbox count:", boxes.count())
    labels = page.locator("input[type='checkbox']")
    print("input checkbox count:", labels.count())
    for i in range(labels.count()):
        el = labels.nth(i)
        print("box", i, "checked=", el.is_checked(), "aria=", el.get_attribute("aria-label"))
    expand = page.locator("xpath=//button[contains(., 'Развернуть')]")
    print("expand count:", expand.count())
    if expand.count() >= 1:
        page.wait_for_timeout(2500)
        expand.first.click()
        page.wait_for_timeout(3000)
        labels = page.locator("input[type='checkbox']")
        print("after expand input checkbox count:", labels.count())
        for i in range(labels.count()):
            el = labels.nth(i)
            print("box", i, "checked=", el.is_checked(), "aria=", el.get_attribute("aria-label"))
        page.wait_for_timeout(2000)
        labels = page.locator("input[type='checkbox']")
        print("after settle count:", labels.count())
        for i in range(labels.count()):
            el = labels.nth(i)
            print("box", i, "checked=", el.is_checked(), "aria=", el.get_attribute("aria-label"))
    # dump the panel region text
    html = page.content()
    idx = html.find("Резюме с этим местом работы")
    print("---- panel html ----")
    print(html[idx - 200 : idx + 6000] if idx != -1 else "panel marker not found")
    with open("/tmp/panel_956.html", "w") as f:
        f.write(html)
    browser.close()
