"""Read-only drill #956 stage 2: replicate the full add-row flow (fills,
panel uncheck clicks, month select) WITHOUT save; dump field state after
each stage."""

from playwright.sync_api import sync_playwright

STATE = "data/storage_state/hh_session.json"
RESUME = "0000111122223333444455556666777788889999"
COMPANY = "[data-qa*='resume-profile-experience-specific-company-input']"
POSITION = "[data-qa*='resume-profile-experience-specific-position-input']"


def state(page, tag):
    comp = page.locator(COMPANY)
    pos = page.locator(POSITION)
    year = page.locator("[data-qa='resume-profile-experience-specific-year-input']")
    months = page.locator("[data-qa='magritte-select-activator']")
    desc = page.locator(
        "[data-qa*='resume-profile-experience-specific-description-input'], textarea"
    )
    print(
        f"[{tag}] company={comp.first.input_value()!r} position={pos.first.input_value()!r} "
        f"year={year.first.input_value() if year.count() else 'N/A'!r} "
        f"months={months.count()} desc={desc.count()}"
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=STATE)
    page = ctx.new_page()
    page.goto(f"https://hh.ru/resume/{RESUME}", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.locator("[data-qa='resume-list-card-experience'] [data-qa='link']").click()
    page.wait_for_timeout(4000)
    state(page, "after open")
    page.locator(COMPANY).first.fill("ТСЖ «Зелёный квартал»")
    page.locator(POSITION).first.fill("Дворник")
    state(page, "after fills t0")
    page.wait_for_timeout(2500)
    state(page, "after 2.5s")
    # open start-month combobox and pick Январь
    act = page.locator("[data-qa='magritte-select-activator']")
    print("activators:", act.count())
    if act.count() >= 1:
        act.first.click()
        page.wait_for_timeout(1500)
        opts = page.locator("[data-qa='magritte-select-option-01']")
        print("option01 count:", opts.count())
        if opts.count():
            opts.first.click()
            page.wait_for_timeout(1000)
    state(page, "after month pick")
    # panel: expand + uncheck one other checkbox
    boxes = page.locator("input[type='checkbox'][aria-label]")
    print("panel boxes:", boxes.count())
    if boxes.count() > 1:
        boxes.nth(1).click()  # uncheck some other resume
        page.wait_for_timeout(1500)
    state(page, "after panel uncheck")
    act = page.locator("[data-qa='magritte-select-activator']")
    for i in range(act.count()):
        print(f"[triggers {i}]", repr(act.nth(i).inner_text()))
    page.wait_for_timeout(3000)
    state(page, "after 3s more")
    act = page.locator("[data-qa='magritte-select-activator']")
    for i in range(act.count()):
        print(f"[triggers+3s {i}]", repr(act.nth(i).inner_text()))
    browser.close()
