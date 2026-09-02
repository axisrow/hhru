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


def test_recommendation_mapping_uses_current_semantic_labels() -> None:
    page = MagicMock()
    name = MagicMock()
    position = MagicMock()
    company = MagicMock()
    # exact — keyword-only в реальном Page.get_by_label; лямбда повторяет это,
    # чтобы MagicMock не маскировал позиционный вызов (cycle-review PR #410
    # round 2, Codex: AST-контракт не видит MagicMock-фейки, здесь сигнатура
    # держится вручную).
    page.get_by_label.side_effect = lambda label, *, exact: {
        "Имя человека": name,
        "Должность": position,
    }[label]
    name.count.return_value = 1
    position.count.return_value = 1
    company.count.return_value = 1
    page.locator.return_value = company

    resume_sections._fill_recommendation_row(
        page, Recommendation(text="", company="Acme", name="Ada", position="Reviewer")
    )

    name.fill.assert_called_once_with("Ada")
    position.fill.assert_called_once_with("Reviewer")
    company.fill.assert_called_once_with("Acme")


def test_recommendation_text_fails_closed_when_current_form_has_no_text_field():
    with pytest.raises(PlaywrightError, match="не содержит поля текста"):
        resume_sections._fill_recommendation_row(
            MagicMock(), Recommendation(text="unsupported", company="Acme")
        )


def test_recommendation_text_is_rejected_before_opening_editor(monkeypatch) -> None:
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.return_value = trigger
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(recommendations=[Recommendation("unsupported", "Acme")]),
        dry_run=True,
    )

    assert "не содержит поля текста" in errors[0]
    trigger.nth.assert_not_called()


def test_recommendation_dry_run_cancels_partial_editor(monkeypatch) -> None:
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_attestations = MagicMock()
    no_attestations.count.return_value = 0
    partial_cancel = MagicMock()
    partial_cancel.count.return_value = 1
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_attestations,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: trigger,
        "[data-qa='resume-partial-edit-cancel']": partial_cancel,
        "input[name='company']": ready,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_recommendation_row", lambda *_args: MagicMock())

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(recommendations=[Recommendation("", "Acme")]),
        dry_run=True,
    )

    assert errors == []
    partial_cancel.click.assert_called_once_with()


def test_save_wait_timeout_is_recorded_as_row_error_not_raised(monkeypatch) -> None:
    """#331 (codex+claude): an uncaught wait for editor close after save must not abort apply_plan."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_recommendations = MagicMock()
    no_recommendations.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    save.wait_for.side_effect = PlaywrightError("timeout waiting for editor to close")
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
        f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']": ready,
    }[selector]
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


def test_save_click_error_is_recorded_as_row_error_not_raised(monkeypatch) -> None:
    """#331 cycle-review round 3: save.click() itself must be guarded too, not
    only the subsequent wait_for — an element-detached/navigation error from
    the click must not propagate out of _apply_rows/apply_plan."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_recommendations = MagicMock()
    no_recommendations.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    save.click.side_effect = PlaywrightError("element is not attached to the DOM")
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
        f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']": ready,
    }[selector]
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
    save.wait_for.assert_not_called()


def test_cancel_click_error_is_recorded_as_row_error_not_raised(monkeypatch) -> None:
    """#331 cycle-review round 3: cancel.click() in the dry-run branch must be
    guarded too, matching the fail-closed convention used for every other
    Playwright action in this loop."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_attestations = MagicMock()
    no_attestations.count.return_value = 0
    partial_cancel = MagicMock()
    partial_cancel.count.return_value = 1
    partial_cancel.click.side_effect = PlaywrightError("element is not attached to the DOM")
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_attestations,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: trigger,
        "[data-qa='resume-partial-edit-cancel']": partial_cancel,
        "input[name='company']": ready,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_recommendation_row", lambda *_args: MagicMock())

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(recommendations=[Recommendation("", "Acme")]),
        dry_run=True,
    )

    assert len(errors) == 1
    assert "отмена" in errors[0] or "recommendations" in errors[0]


def test_save_confirmation_does_not_rely_on_url_already_matched(monkeypatch) -> None:
    """#331 (codex): apply_plan already navigated to /resume/{resume_id} before any
    row save, so waiting on that same URL after save.click() would resolve
    immediately regardless of whether the save actually persisted. The fix must
    wait for a signal specific to this save (the editor closing), not the URL."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    no_attestations = MagicMock()
    no_attestations.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_attestations,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: trigger,
        "input[name='company']": ready,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_recommendation_row", lambda *_args: save)

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(recommendations=[Recommendation("", "Acme")]),
        dry_run=False,
    )

    assert errors == []
    # The confirmation must be driven by the save locator itself (the editor
    # closing), never by page.wait_for_url — the URL never changes here.
    save.wait_for.assert_called_once_with(state="hidden", timeout=resume_sections.SAVE_TIMEOUT_MS)
    page.wait_for_url.assert_not_called()


def test_unconfirmed_save_stops_block_instead_of_clicking_next_row(monkeypatch) -> None:
    """#331 (codex+claude): a save.wait_for timeout means the editor is likely
    still open, exactly like the ambiguous save/cancel branches — the block
    must stop instead of clicking the next row's trigger against a stale,
    unresolved editor state."""
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 2
    no_recommendations = MagicMock()
    no_recommendations.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    save.wait_for.side_effect = PlaywrightError("timeout waiting for editor to close")
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
        f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']": ready,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_attestation_row", lambda *_args: save)

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
    # Only the first row's trigger was clicked — the block stopped instead of
    # querying the second row's trigger against a still-open editor.
    assert trigger.nth.call_count == 1


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
    ready = MagicMock()
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: trigger,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_recommendations,
        f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']": ready,
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


def test_attestation_form_uses_live_confirmed_selectors() -> None:
    """#773: the historical ``profile-education-attestation-*`` candidates do not
    exist on hh.ru (count=0). The live form at
    /resume/edit/<id>/attestationEducation exposes its own namespace instead
    (live probe 2026-08-30 on a draft resume).

    The third field is pinned deliberately: hh.ru labels it "Специализация" but
    names the attribute ``-result``, so field order follows the Attestation
    dataclass, not the attribute wording.
    """
    assert resume_sections.ATTESTATION_FIELDS == (
        "resume-attestation-education-input-name",
        "resume-attestation-education-input-organization",
        "resume-attestation-education-input-result",
        "resume-attestation-education-input-year",
    )
    assert not any(
        field.startswith("profile-education-") for field in resume_sections.ATTESTATION_FIELDS
    )


def test_attestation_row_saves_through_partial_edit_button() -> None:
    """#773: the attestation editor is a resume-scoped partial edit, so it uses
    ``resume-partial-edit-save`` — ``profile-layout-save-button`` is count=0 there
    and belongs to the profile-scoped primary-education form instead."""
    page = MagicMock()
    save = MagicMock()
    page.locator.return_value = save

    returned = resume_sections._fill_attestation_row(
        page, resume_sections.Attestation("AWS", "Amazon", "Cloud", "2024")
    )

    assert returned is save
    assert page.locator.call_args_list[-1].args[0] == "[data-qa='resume-partial-edit-save']"


@pytest.mark.parametrize(
    ("block", "path", "item"),
    [
        (
            "attestations",
            "attestationEducation",
            resume_sections.Attestation("AWS", "Amazon", "Cloud", "2024"),
        ),
        (
            "recommendations",
            "recommendation",
            Recommendation("", "Acme", "Ada", "Reviewer"),
        ),
    ],
)
def test_empty_section_opens_first_row_via_resume_scoped_route(
    monkeypatch, block, path, item
) -> None:
    """#922: an empty block has no row trigger, so the first row uses the
    confirmed resume-scoped editor route instead of inventing an add click."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    no_rows = MagicMock()
    no_rows.count.return_value = 0
    empty_marker = MagicMock()
    empty_marker.count.return_value = 1
    ready = MagicMock()
    cancel = MagicMock()
    cancel.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_rows,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_rows,
        resume_sections.EMPTY_SECTION_MARKERS["attestations"]: empty_marker,
        resume_sections.EMPTY_SECTION_MARKERS["recommendations"]: empty_marker,
        (
            f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']"
            if block == "attestations"
            else "input[name='company']"
        ): ready,
        "[data-qa='resume-partial-edit-cancel']": cancel,
    }[selector]
    visited = []

    def goto(_page, url):
        visited.append(url)
        page.url = url

    monkeypatch.setattr(resume_sections, "goto_hh", goto)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(
        resume_sections,
        "_fill_attestation_row" if block == "attestations" else "_fill_recommendation_row",
        lambda *_args: MagicMock(),
    )

    plan = (
        ResumeSectionsPlan(attestations=[item])
        if block == "attestations"
        else ResumeSectionsPlan(recommendations=[item])
    )
    errors = apply_plan(page, "resume-id", plan, dry_run=True)

    assert errors == []
    assert f"https://hh.ru/resume/edit/resume-id/{path}" in visited
    no_rows.nth.assert_not_called()
    cancel.click.assert_called_once_with()


def test_both_empty_sections_reset_to_resume_before_each_block(monkeypatch) -> None:
    """#922: the second empty block must be inspected on the resume page,
    not on the first block's editor route."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    no_rows = MagicMock()
    no_rows.count.return_value = 0
    empty_marker = MagicMock()
    empty_marker.count.return_value = 1
    ready = MagicMock()
    cancel = MagicMock()
    cancel.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_rows,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_rows,
        resume_sections.EMPTY_SECTION_MARKERS["attestations"]: empty_marker,
        resume_sections.EMPTY_SECTION_MARKERS["recommendations"]: empty_marker,
        f"[data-qa='{resume_sections.ATTESTATION_FIELDS[0]}']": ready,
        "input[name='company']": ready,
        "[data-qa='resume-partial-edit-cancel']": cancel,
    }[selector]
    visited = []

    def goto(_page, url):
        visited.append(url)
        page.url = url

    monkeypatch.setattr(resume_sections, "goto_hh", goto)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_sections, "_fill_attestation_row", lambda *_args: MagicMock())
    monkeypatch.setattr(resume_sections, "_fill_recommendation_row", lambda *_args: MagicMock())

    plan = ResumeSectionsPlan(
        attestations=[resume_sections.Attestation("AWS", "Amazon", "Cloud", "2024")],
        recommendations=[Recommendation("", "Acme", "Ada", "Reviewer")],
    )
    errors = apply_plan(page, "resume-id", plan, dry_run=True)

    assert errors == []
    assert visited == [
        "https://hh.ru/resume/resume-id",
        "https://hh.ru/resume/resume-id",
        "https://hh.ru/resume/edit/resume-id/attestationEducation",
        "https://hh.ru/resume/resume-id",
        "https://hh.ru/resume/edit/resume-id/recommendation",
    ]
    no_rows.nth.assert_not_called()
    assert cancel.click.call_count == 2


@pytest.mark.parametrize("marker_count", [0, 2])
def test_empty_section_requires_unique_live_marker(monkeypatch, marker_count) -> None:
    """#922: zero triggers without exactly one empty marker are indeterminate."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    no_rows = MagicMock()
    no_rows.count.return_value = 0
    marker = MagicMock()
    marker.count.return_value = marker_count
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_rows,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_rows,
        resume_sections.EMPTY_SECTION_MARKERS["attestations"]: marker,
    }[selector]
    monkeypatch.setattr(resume_sections, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(
            attestations=[resume_sections.Attestation("AWS", "Amazon", "Cloud", "2024")]
        ),
        dry_run=True,
    )

    assert len(errors) == 1
    assert "пустой блок не подтверждён однозначно" in errors[0]


def test_empty_section_route_guard_rejects_other_resume(monkeypatch) -> None:
    """#922: direct first-row navigation must retain resume identity binding."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    no_rows = MagicMock()
    no_rows.count.return_value = 0
    marker = MagicMock()
    marker.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_sections.RESUME_EDIT_BUTTON["attestations"]: no_rows,
        resume_sections.RESUME_EDIT_BUTTON["recommendations"]: no_rows,
        resume_sections.EMPTY_SECTION_MARKERS["attestations"]: marker,
    }[selector]

    def goto(_page, url):
        page.url = url.replace("resume-id", "other-resume")

    monkeypatch.setattr(resume_sections, "goto_hh", goto)
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(resume_sections, "has_login_form", lambda _page: False)

    errors = apply_plan(
        page,
        "resume-id",
        ResumeSectionsPlan(
            attestations=[resume_sections.Attestation("AWS", "Amazon", "Cloud", "2024")]
        ),
        dry_run=True,
    )

    assert len(errors) == 1
    assert "первая строка открыта не для того резюме" in errors[0]
