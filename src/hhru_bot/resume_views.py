"""Read-only parser for the employer resume-view history page (#415)."""

from __future__ import annotations

import re
from datetime import datetime

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
) -> list[dict]:
    """Parse SSR history; raise instead of treating schema drift as empty data."""
    state = parse_initial_state(html)
    history = _find_history(state)
    if history is None or not isinstance(history.get("historyViews"), list):
        raise ValueError("SSR applicantResumeViewHistory.historyViews недоступен")

    result = []
    for entry in history["historyViews"]:
        if not isinstance(entry, dict):
            raise ValueError("SSR history contains an invalid view entry")
        viewed_at = _value(entry, "date", "viewedAt", "viewDate", "createdAt")
        if viewed_at is None:
            raise ValueError("SSR history view has no date")
        employer_id = _value(entry, "employerId", "employer_id", "companyId")
        employer = _value(entry, "employerName", "employer", "companyName", "name")
        if employer is None:
            source_id = _value(entry, "id", "viewId", "eventId")
            if source_id is None and employer_id is None:
                raise ValueError("SSR hidden-employer view has no stable identity")
            employer = f"(скрыт:{source_id or employer_id})"
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
                viewed_at = times[0].get_attribute("datetime")
        if not employer:
            employers = row.locator("[data-qa*='employer'], a[href*='/employer/']").all()
            if len(employers) == 1:
                employer = employers[0].inner_text().strip()
        employer_id = None
        if not employer:
            invalid = True
            continue
        if not viewed_at:
            invalid = True
            continue
        try:
            viewed_at = datetime.fromisoformat(viewed_at.replace("Z", "+00:00")).isoformat()
        except ValueError:
            invalid = True
            continue
        if not row.get_attribute("data-employer-name"):
            employers = row.locator("[data-qa*='employer'], a[href*='/employer/']").all()
            if len(employers) == 1:
                href = employers[0].get_attribute("href") or ""
                match = re.search(r"/employer/([^/?#]+)", href)
                employer_id = match.group(1) if match else None
        result.append(
            {
                "resume_id": resume_id,
                "employer_id": employer_id,
                "employer": employer,
                "viewed_at": viewed_at,
            }
        )
        if limit is not None and len(result) >= limit:
            break
    if invalid:
        raise ValueError("DOM history contains an unparseable view row")
    return result
