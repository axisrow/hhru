"""Read-only probe: what education markers render on a fresh draft resume (#857)."""

from playwright.sync_api import sync_playwright

RESUME_URL = "https://hh.ru/resume/0000111122223333444455556666777788889999"
STATE = "data/accounts/default/storage_state/hh_session.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

MARKERS = [
    "resume-edit-button-additionalEducation-0",
    "resume-list-card-additionalEducation",
    "resume-edit-button-education-0",
    "resume-list-card-education",
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=STATE, user_agent=UA)
    page = ctx.new_page()
    page.goto(RESUME_URL, wait_until="commit", timeout=90000)
    page.wait_for_timeout(12000)  # let the SPA hydrate
    print("URL:", page.url)
    for m in MARKERS:
        attr = f"[data-qa='{m}']"
        print(f"{m}: count={page.locator(attr).count()}")
        for i in range(min(page.locator(attr).count(), 3)):
            el = page.locator(attr).nth(i)
            print(
                "   visible:",
                el.is_visible(),
                "| text:",
                el.inner_text()[:120].replace("\n", " | "),
            )
    # any data-qa containing ducation or dditional
    qa = page.eval_on_selector_all(
        "[data-qa]",
        "els => els.map(e => e.getAttribute('data-qa')).filter(v => /[Ee]ducation|dditional/.test(v))",
    )
    print("education-related data-qa:", sorted(set(qa)))
    browser.close()
