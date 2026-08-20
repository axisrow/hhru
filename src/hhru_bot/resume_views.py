"""Read-only parser for the employer resume-view history page (#415)."""

from __future__ import annotations

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

    SSR can render the same instant differently across entries (e.g.
    trailing ``Z`` vs ``+00:00``, the same instant in a different UTC
    offset, or an offset omitted entirely); without this, the same event
    could dedup as two rows depending on which spelling was present
    (#428 review). A naive value (no offset at all, e.g. bare
    "2026-08-20T10:00:00") is treated as UTC, matching hh.ru's
    Z-suffixed timestamps.
    """
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


def parse_resume_view_history(
    html: str,
    resume_id: str,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Parse SSR history; raise instead of treating schema drift as empty data.

    NOT verified against live DOM (#428 review, round 13): this trusts the
    caller's `resume_id` for every parsed entry without cross-checking it
    against a resume identity signal inside the SSR payload itself. Codex
    flagged the risk (route drift / a stale page returning a different
    resume's history could get recorded under the wrong resume_id) at 0.9
    confidence, but implementing a check now would mean guessing an SSR
    field name with no live dump to confirm it exists — exactly the
    `_form_scope()` trap CLAUDE.md documents (a wrong guess either fails
    closed on every run, or never fires and gives false confidence). Before
    the first live `resume-views` run: open the page in a real browser
    (F12 → Elements/Network), confirm whether `applicantResumeViewHistory`
    carries a resume identity field, and wire the check here if it does —
    same convention as the pending `bump` selector check in CLAUDE.md.
    """
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
        # Require source_id or employer_id regardless of whether `employer`
        # (the mutable display name) is present (#428 review, round 12): an
        # entry with only a name and neither ID would get view_key='' in
        # history.py, and TWO SUCH entries — different employers or repeat
        # views of the same one — would silently collapse into one row via
        # INSERT OR IGNORE. Fail closed instead of persisting an ambiguous
        # identity (CLAUDE.md decision #5).
        if source_id is None and employer_id is None:
            raise ValueError("SSR view has no stable identity (no source_id or employer_id)")
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
