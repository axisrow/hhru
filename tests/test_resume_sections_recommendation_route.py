"""Regression test for the recommendation route guard's resume-id binding
(#368 cycle-review round 1, codex finding).

Before the fix, SECTION_ROUTES["recommendations"] matched
/resume/edit/<ANY id>/recommendation/<id> — a stale or misdirected edit link
for a DIFFERENT resume would pass the route guard in _apply_rows even though
wrong_route_error's message claims resume identity was verified. The route
must be bound to the specific requested resume_id.
"""

from __future__ import annotations

import pytest

from hhru_bot.resume_sections import _recommendation_route

pytestmark = pytest.mark.unit


def test_recommendation_route_matches_only_the_requested_resume():
    route = _recommendation_route("35661ef3ff10f971a70039ed1f57656d684c54")

    assert route.fullmatch("/resume/edit/35661ef3ff10f971a70039ed1f57656d684c54/recommendation/abc")


def test_recommendation_route_rejects_a_different_resume_id():
    route = _recommendation_route("35661ef3ff10f971a70039ed1f57656d684c54")

    # A stale/misdirected edit link pointing at a DIFFERENT resume must not
    # pass the guard, even though the path shape otherwise matches.
    assert route.fullmatch("/resume/edit/OTHER-RESUME-999/recommendation/abc") is None


def test_recommendation_route_escapes_regex_metacharacters_in_resume_id():
    # resume_id is attacker/server controlled input reflected into a regex;
    # a resume_id containing regex metacharacters must not widen the match.
    route = _recommendation_route("a.b")

    assert route.fullmatch("/resume/edit/a.b/recommendation/abc")
    assert route.fullmatch("/resume/edit/aXb/recommendation/abc") is None
