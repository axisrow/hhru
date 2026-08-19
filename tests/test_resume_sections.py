"""Pure safety and LLM-contract tests for additional resume sections."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

import hhru_bot.resume_sections as resume_sections
from hhru_bot.config import ConfigError
from hhru_bot.config_sections.resume_sections import parse_resume_sections
from hhru_bot.resume_sections import Recommendation, ResumeSectionsPlan, apply_plan, parse_plan

pytestmark = pytest.mark.unit


def test_config_rejects_unconfirmed_blocks() -> None:
    with pytest.raises(ConfigError):
        parse_resume_sections({"blocks": ["certificates"]}, "resumes[0].resume_sections")


def test_plan_parses_only_requested_typed_blocks() -> None:
    plan = parse_plan(
        '{"attestations": [{"name": "AWS", "organization": "Amazon", '
        '"specialty": "Cloud", "year": "2024"}], '
        '"recommendations": [{"text": "Great", "company": "Acme"}]}',
        ["recommendations"],
    )
    assert plan.attestations == []
    assert plan.recommendations[0].company == "Acme"


def test_malformed_llm_output_is_fail_closed() -> None:
    plan = parse_plan("not json", ["attestations", "recommendations"])
    assert not plan.attestations
    assert not plan.recommendations
    assert plan.skipped


def test_recommendation_dry_run_cancels_partial_editor(monkeypatch) -> None:
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_attestations = MagicMock()
    no_attestations.count.return_value = 0
    partial_cancel = MagicMock()
    partial_cancel.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_attestations,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: trigger,
        "[data-qa='resume-partial-edit-cancel']": partial_cancel,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_recommendation_row", lambda *_args: MagicMock())

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(recommendations=[Recommendation("Text", "Acme")]),
        dry_run=True,
    )

    assert errors == []
    partial_cancel.click.assert_called_once_with()


def test_save_wait_timeout_is_recorded_as_row_error_not_raised(monkeypatch) -> None:
    """#331 (codex+claude): an uncaught wait_for_url after save must not abort apply_plan."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_recommendations = MagicMock()
    no_recommendations.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
    }[selector]
    page.wait_for_url.side_effect = PlaywrightError("timeout waiting for navigation")
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_attestation_row", lambda *_args: save)

    from hhru_bot.resume_sections import Attestation

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(attestations=[Attestation("AWS", "Amazon", "Cloud", "2024")]),
        dry_run=False,
    )

    assert len(errors) == 1
    assert "сохранение" in errors[0] or "attestations" in errors[0]
    save.click.assert_called_once_with()


def test_ambiguous_save_button_stops_block_instead_of_leaving_editor_open(monkeypatch) -> None:
    """#331: an ambiguous save/cancel match must not query the next row's
    trigger while the current row editor is still open."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 2
    no_recommendations = MagicMock()
    no_recommendations.count.return_value = 0
    ambiguous_save = MagicMock()
    ambiguous_save.count.return_value = 2
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_attestation_row", lambda *_args: ambiguous_save)

    from hhru_bot.resume_sections import Attestation

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(
            attestations=[
                Attestation("AWS", "Amazon", "Cloud", "2024"),
                Attestation("GCP", "Google", "Cloud", "2023"),
            ]
        ),
        dry_run=False,
    )

    assert len(errors) == 1
    assert trigger.nth.call_count == 1
