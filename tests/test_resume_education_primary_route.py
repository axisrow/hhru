"""PRIMARY_ROUTE regex regression (#814).

hh.ru opens two different URL shapes for the primary-education editor
depending on whether the section already has an entry (live DOM, #814):

* empty section (via PRIMARY_ADD)         -> /profile/edit/primaryEducation
* existing entry (via PRIMARY_TRIGGER-0)  -> /profile/edit/primaryEducation/{entry_id}

PRIMARY_ROUTE must accept both (the #794 no-tail case is a regression guard)
while still rejecting an unrelated path -- the actual guard against opening a
*different resume's* form is the separate `expected_query={"resumeFrom": ...}`
check in `_edit_block` (browser.py's `open_hydrated_resume_editor`), not this
regex. This regex only has to keep recognizing the primary-education screen,
not double as an identity check.
"""

from __future__ import annotations

import pytest

from hhru_bot.resume_education import PRIMARY_ROUTE

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path",
    [
        "/profile/edit/primaryEducation",
        "/profile/edit/primaryEducation/104508559",
    ],
)
def test_primary_route_matches_empty_and_existing_entry_shapes(path):
    assert PRIMARY_ROUTE.fullmatch(path)


@pytest.mark.parametrize(
    "path",
    [
        "/profile/edit/additionalEducation",
        "/profile/edit/additionalEducation/104508559",
        "/profile/edit/primaryEducationExtra",
        "/profile/edit/primaryEducation/104508559/extra",
    ],
)
def test_primary_route_rejects_unrelated_paths(path):
    assert not PRIMARY_ROUTE.fullmatch(path)
