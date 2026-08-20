"""Read-only parser for the employer resume-view history page (#415)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

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


def _canonicalize_viewed_at(raw: str) -> str:
    """Normalize a view timestamp to one canonical ISO spelling.

    SSR and DOM sources render the same instant differently (e.g. trailing
    ``Z`` vs ``+00:00``, or the same instant in a different UTC offset);
    without this, the same event dedups as two rows depending on which
    source captured it first, or which offset it was rendered in
    (#428 review).
    """
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


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
        try:
            viewed_at = _canonicalize_viewed_at(str(viewed_at))
        except ValueError as exc:
            raise ValueError("SSR history view has an unparseable date") from exc
        employer_id = _value(entry, "employerId", "employer_id", "companyId")
        employer = _value(entry, "employerName", "employer", "companyName", "name")
        # Prefer the SSR per-view event ID whenever it's present, even for an
        # identified employer: employer_id + date-only viewed_at alone cannot
        # distinguish two separate views of the same employer on the same day
        # (SSR dates often carry no time-of-day), so relying on employer_id as
        # the sole identity would silently drop the second view (#428 review).
        source_id = _value(entry, "id", "viewId", "eventId")
        if employer is None and source_id is None and employer_id is None:
            raise ValueError("SSR hidden-employer view has no stable identity")
        result.append(
            {
                "resume_id": str(resume_id),
                "employer_id": None if employer_id is None else str(employer_id),
                "employer": None if employer is None else str(employer),
                # Distinct view events need their own identity for dedup — see
                # history.py's resume_views.view_key, which prefers source_id
                # over employer_id precisely to keep same-employer/same-day
                # views distinct. Never encode this into `employer` (#428
                # review): it corrupts the "Топ работодателей" aggregation and
                # leaks internal IDs.
                "source_id": None if source_id is None else str(source_id),
                "viewed_at": viewed_at,
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
            viewed_at = _canonicalize_viewed_at(viewed_at)
        except ValueError:
            invalid = True
            continue
        # Extract employer_id from the link whenever it's present, regardless of
        # whether the name came from data-employer-name or the link's own text
        # (#428 review): the guard used to skip this whenever data-employer-name
        # was set, leaving employer_id NULL and letting a later SSR scrape of the
        # same view insert a duplicate row under the resume_views dedup key.
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
                "source_id": None,
                "viewed_at": viewed_at,
            }
        )
        if limit is not None and len(result) >= limit:
            break
    if invalid:
        raise ValueError("DOM history contains an unparseable view row")
    return result
