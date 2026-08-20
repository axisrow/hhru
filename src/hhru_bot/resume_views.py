"""Read-only parser for the employer resume-view history page (#415)."""

from __future__ import annotations

from .negotiations_probe import parse_initial_state


def has_next_page(page, page_num: int) -> bool:
    """Return whether the rendered history pager confirms another page."""
    next_link = page.locator("[data-qa*='pager-next'], [data-qa*='pagination-next'], a[rel='next']")
    if next_link.count() > 0:
        return True
    pages = page.locator("[data-qa*='pager-page'], [data-qa*='pagination-page']")
    for index in range(pages.count()):
        try:
            if int(pages.nth(index).inner_text().strip()) > page_num + 1:
                return True
        except ValueError:
            continue
    return False


def _find_history(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "applicantResumeViewHistory" and isinstance(child, dict):
                return child
            found = _find_history(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_history(child)
            if found is not None:
                return found
    return None


def _value(entry: dict, *names):
    for name in names:
        value = entry.get(name)
        if value not in (None, ""):
            return value
    return None


def parse_resume_view_history(
    html: str,
    resume_id: str,
    *,
    limit: int | None = None,
    hidden_offsets: dict[str, int] | None = None,
) -> list[dict]:
    """Parse SSR history; raise instead of treating schema drift as empty data."""
    state = parse_initial_state(html)
    history = _find_history(state)
    if history is None or not isinstance(history.get("historyViews"), list):
        raise ValueError("SSR applicantResumeViewHistory.historyViews недоступен")

    result = []
    hidden_by_date = hidden_offsets if hidden_offsets is not None else {}
    for entry in history["historyViews"]:
        if not isinstance(entry, dict):
            continue
        viewed_at = _value(entry, "date", "viewedAt", "viewDate", "createdAt")
        if viewed_at is None:
            continue
        employer_id = _value(entry, "employerId", "employer_id", "companyId")
        employer = _value(entry, "employerName", "employer", "companyName", "name")
        if employer is None:
            hidden_by_date[str(viewed_at)] = hidden_by_date.get(str(viewed_at), 0) + 1
            employer = f"(скрыт #{hidden_by_date[str(viewed_at)]})"
        result.append(
            {
                "resume_id": str(resume_id),
                "employer_id": None if employer_id is None else str(employer_id),
                "employer": None if employer is None else str(employer),
                "viewed_at": str(viewed_at),
            }
        )
        if limit is not None and len(result) >= limit:
            break
    return result


def parse_resume_view_history_dom(page, resume_id: str, *, limit: int | None = None) -> list[dict]:
    """Parse only DOM rows with independently exposed date and employer fields."""
    rows = page.locator("[data-qa*='resume-view'], [data-qa*='view-history']").all()
    result = []
    invalid = False
    for row in rows:
        viewed_at = row.get_attribute("data-viewed-at")
        employer = row.get_attribute("data-employer-name")
        if not viewed_at:
            times = row.locator("time").all()
            if len(times) == 1:
                viewed_at = times[0].get_attribute("datetime") or times[0].inner_text().strip()
        if not employer:
            employers = row.locator("[data-qa*='employer'], a[href*='/employer/']").all()
            if len(employers) == 1:
                employer = employers[0].inner_text().strip()
        if not viewed_at or not employer:
            invalid = True
            continue
        result.append(
            {
                "resume_id": resume_id,
                "employer_id": None,
                "employer": employer,
                "viewed_at": viewed_at,
            }
        )
        if limit is not None and len(result) >= limit:
            break
    if invalid:
        raise ValueError("DOM history contains an unparseable view row")
    return result
