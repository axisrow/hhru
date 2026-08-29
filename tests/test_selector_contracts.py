from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "selector_contracts.py"
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "selector-refresh.yml"
SPEC = importlib.util.spec_from_file_location("selector_contracts", SCRIPT_PATH)
assert SPEC and SPEC.loader
contracts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contracts
SPEC.loader.exec_module(contracts)

pytestmark = pytest.mark.unit

AUDIT_GROUPS = {
    "resume_page",
    "resume_list",
    "resume_experience",
    "resume_visibility",
    "resume_rename",
    "account_profile",
    "competitor_resume",
    "resume_education",
    "resume_position",
}
AUDIT_STATUSES = {
    "reference_binding",
    "intentionally_local",
    "not_implemented_upstream",
    "needs_live_evidence",
}
AUDIT_FIELDS = (
    "origin",
    "verification",
    "evidence",
    "last_verified_at",
    "verified_flow",
    "verified_by",
)


ISSUE_628_LOGICAL_IDS = {
    "professional_roles.FILTER_TRIGGER",
    "professional_roles.TREE_CATEGORY_INPUT",
    "professional_roles.TREE_CHEVRON",
    "professional_roles.TREE_INPUT",
    "professional_roles.TREE_INPUT_ANY",
    "professional_roles.TREE_LABEL",
    "resume_sections.ATTESTATION_SELECTOR.0",
    "resume_sections.ATTESTATION_SELECTOR.1",
    "resume_sections.ATTESTATION_SELECTOR.2",
    "resume_sections.ATTESTATION_SELECTOR.3",
    "resume_sections.RESUME_EDIT_BUTTON.attestations",
    "resume_sections.RESUME_EDIT_BUTTON.recommendations",
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.0.1",
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.1.1",
    "apply.antibot.ANTIBOT_MARKER_SELECTORS.2.1",
    "browser.LOGIN_FORM",
    "create_resume.TREE_ITEM_TEXT",
}


def test_issue_628_selector_contract_coverage_is_explicit():
    selectors = contracts.load_catalog()["selectors"]
    assert set(ISSUE_628_LOGICAL_IDS) <= selectors.keys()
    for logical_id in ISSUE_628_LOGICAL_IDS:
        row = selectors[logical_id]
        assert all(row.get(field) not in (None, "", {}) for field in AUDIT_FIELDS), logical_id
        assert row.get("coverage_status") in AUDIT_STATUSES, logical_id
        assert row["origin"] != "llm_hypothesis", logical_id
        if logical_id.startswith("apply.antibot."):
            assert row["verification"] in {"unverified", "unavailable"}, logical_id


VACANCY_CONSENSUS_PORTS = {
    "vacancy_page.VACANCY_DESCRIPTION": {
        "value": '[data-qa="vacancy-description"]',
        "references": {"steev", "yamakayama"},
    },
    "vacancy_page.VACANCY_EXPERIENCE": {
        "value": '[data-qa="vacancy-experience"]',
        "references": {"steev", "yamakayama"},
    },
    "vacancy_page.VACANCY_VIEW_EMPLOYMENT_MODE": {
        "value": '[data-qa="vacancy-view-employment-mode"]',
        "references": {"steev", "yamakayama"},
    },
    "vacancy_page.VACANCY_VIEW_LOCATION": {
        "value": '[data-qa="vacancy-view-location"]',
        "references": {"steev", "tgeruzov"},
    },
    "vacancy_page.VACANCY_VIEW_RAW_ADDRESS": {
        "value": '[data-qa="vacancy-view-raw-address"]',
        "references": {"steev", "tgeruzov"},
    },
}


VACANCY_CANDIDATE_VALUES = {
    '[data-qa="vacancy-description"]',
    '[data-qa="vacancy-experience"]',
    '[data-qa="vacancy-response-letter-submit"]',
    '[data-qa="vacancy-response-link-bottom"]',
    '[data-qa="vacancy-view-employment-mode"]',
    '[data-qa="vacancy-view-location"]',
    '[data-qa="vacancy-view-raw-address"]',
    'h1[data-qa="vacancy-title"]',
}


def _commit_repository(path: Path, selector: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "selectors.py").write_text(f'SELECTOR = "{selector}"\n', encoding="utf-8")
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "selectors.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=selector-test",
            "-c",
            "user.email=selector-test@example.invalid",
            "commit",
            "-qm",
            "selector fixture",
        ],
        check=True,
    )


def test_current_repository_selector_contract_is_self_consistent():
    catalog = contracts.load_catalog()
    assert contracts.verify_catalog(catalog) == []
    assert len(catalog["references"]) == 3
    assert catalog["policy"]["consensus_threshold"] == 2


def test_every_selector_has_canonical_coverage_and_all_groups_are_audited():
    catalog = contracts.load_catalog()
    expected_prefixes = {
        "account_profile.",
        "apply_form.",
        "competitor_resume.",
        "negotiations.",
        "resume_experience.",
        "resume_list.",
        "resume_page.",
        "resume_rename.",
        "resume_visibility.",
        "search_page.",
        "vacancy_page.",
        # selector_groups/login.py retains the historical selectors.* IDs.
        "selectors.",
    }

    assert set(contracts.AUDITED_SELECTOR_GROUP_PREFIXES) == expected_prefixes
    # #746: +17 resume_visibility.* contracts (radio modes, employer-list
    # modal, search/checkbox/save/cancel) confirmed on live authenticated DOM.
    # #745: +10 vacancy_complain.* contracts (report-vacancy exploration).
    assert len(catalog["selectors"]) == 242
    assert all(
        row.get("coverage_status") in contracts.AUDIT_STATUSES
        for row in catalog["selectors"].values()
    )
    for logical_id, row in catalog["selectors"].items():
        if logical_id.startswith(contracts.AUDITED_SELECTOR_GROUP_PREFIXES):
            assert all(
                row.get(field) not in (None, "", {}) for field in contracts.AUDIT_REQUIRED_FIELDS
            )


def test_invalid_coverage_status_fails_catalog_gate():
    catalog = contracts.load_catalog()
    catalog["selectors"]["search_page.VACANCY_CARD"]["coverage_status"] = "needs-live-evidence"

    errors = contracts.verify_catalog(catalog)

    assert "search_page.VACANCY_CARD: invalid coverage_status" in errors


def test_extra_contract_bootstrap_has_catalog_wide_coverage():
    catalog = {"selectors": {}}

    contracts._ensure_extra_contracts(catalog, {})

    assert catalog["selectors"]
    assert all(
        row.get("coverage_status") in contracts.AUDIT_STATUSES
        for row in catalog["selectors"].values()
    )


def test_bootstrap_output_passes_catalog_gate(tmp_path, monkeypatch):
    source_root = tmp_path / "src" / "hhru_bot"
    selector_group = source_root / "selector_groups" / "account_profile.py"
    selector_group.parent.mkdir(parents=True)
    selector_group.write_text("ACCOUNT_NAME = \"[data-qa='account-name']\"\n", encoding="utf-8")
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    monkeypatch.setattr(contracts, "SOURCE_ROOT", source_root)
    monkeypatch.setattr(
        contracts, "GENERATED_PATH", source_root / "selector_groups" / "_generated.py"
    )
    monkeypatch.setattr(contracts, "MATRIX_PATH", tmp_path / "selectors" / "reference-matrix.md")
    monkeypatch.setattr(contracts, "EXTRA_CONTRACTS", {})

    reference_root = tmp_path / "references"
    for config in contracts.REFERENCE_CONFIG.values():
        _commit_repository(reference_root / config["directory"], "[data-qa='account-name']")

    catalog, _ = contracts.build_map(reference_root, tmp_path / "live")
    contracts.GENERATED_PATH.write_text(contracts.render_generated(catalog), encoding="utf-8")
    contracts.MATRIX_PATH.parent.mkdir(parents=True)
    contracts.MATRIX_PATH.write_text(contracts.render_matrix(catalog), encoding="utf-8")

    assert contracts.verify_catalog(catalog) == []
    assert catalog["selectors"]["account_profile.ACCOUNT_NAME"]["coverage_status"] == (
        "reference_binding"
    )


def test_resume_account_competitor_unbound_selectors_have_provenance():
    catalog = contracts.load_catalog()
    rows = {
        logical_id: row
        for logical_id, row in catalog["selectors"].items()
        if logical_id.split(".", 1)[0] in AUDIT_GROUPS
    }

    assert rows
    for logical_id, row in rows.items():
        assert row.get("status") in {
            "reference binding",
            "intentionally local",
            "not implemented upstream",
            "needs-live-evidence",
        }, logical_id
        assert all(row.get(field) not in (None, "", {}) for field in AUDIT_FIELDS), logical_id
        assert "llm_hypothesis" not in row, logical_id
        if not (row.get("bindings") or row.get("sources")):
            assert row["status"] != "reference binding", logical_id


def test_issue_627_resume_education_has_complete_coverage_metadata():
    catalog = contracts.load_catalog()
    rows = {
        logical_id: row
        for logical_id, row in catalog["selectors"].items()
        if logical_id.startswith("resume_education.")
    }
    assert len(rows) == 14
    for logical_id, row in rows.items():
        assert row.get("coverage_status") in AUDIT_STATUSES, logical_id
        assert all(row.get(field) not in (None, "", {}) for field in AUDIT_FIELDS), logical_id
        assert row.get("origin") != "llm_hypothesis", logical_id


@pytest.mark.parametrize("logical_id, expected", VACANCY_CONSENSUS_PORTS.items())
def test_vacancy_consensus_ports_are_exact_reference_literals(logical_id, expected):
    row = contracts.load_catalog()["selectors"][logical_id]

    assert row["decision"] == "consensus"
    assert row["origin"] == "reference_consensus"
    assert row["verification"] == "contract_tested"
    assert row["criticality"] == "read"
    assert set(row["sources"]) == expected["references"]
    for source_entries in row["sources"].values():
        for source in source_entries:
            assert source["file"]
            assert isinstance(source["line"], int)
            assert source["key"]
            assert source["value"] == expected["value"]


def test_every_vacancy_upstream_candidate_has_an_explicit_decision():
    catalog = contracts.load_catalog()
    candidates = {
        contracts.normalize_selector(row["value"]): row
        for row in catalog["upstream_consensus"]
        if contracts.normalize_selector(row["value"])
        in {contracts.normalize_selector(value) for value in VACANCY_CANDIDATE_VALUES}
    }

    assert set(candidates) == {
        contracts.normalize_selector(value) for value in VACANCY_CANDIDATE_VALUES
    }
    for row in candidates.values():
        assert row["decision"] in {"port_exact", "reject"}
        if row["decision"] == "port_exact":
            assert row["logical_id"] in VACANCY_CONSENSUS_PORTS
            assert row["origin"] == "reference_consensus"
            assert row["verification"] == "contract_tested"
        else:
            assert row["reason_code"] in {"duplicate_local_role", "write_risk"}
            assert row["reason"]


def test_refresh_preserves_upstream_candidate_decisions():
    selector = '[data-qa="vacancy-description"]'
    indexes = {
        reference: [
            contracts.SourceSelector(
                selector,
                contracts.normalize_selector(selector),
                f"{reference}.py",
                1,
                f"{reference}#0",
            )
        ]
        for reference in contracts.REFERENCE_CONFIG
    }
    previous = [
        {
            "value": selector,
            "references": ["steev", "yamakayama"],
            "sources": {},
            "decision": "port_exact",
            "logical_id": "vacancy_page.VACANCY_DESCRIPTION",
            "origin": "reference_consensus",
            "verification": "contract_tested",
        }
    ]

    refreshed = contracts._upstream_consensus(indexes, previous)

    assert refreshed[0]["decision"] == "port_exact"
    assert refreshed[0]["logical_id"] == "vacancy_page.VACANCY_DESCRIPTION"
    assert refreshed[0]["origin"] == "reference_consensus"
    assert refreshed[0]["verification"] == "contract_tested"


def test_issue_610_apply_login_negotiations_coverage_is_classified():
    catalog = contracts.load_catalog()
    prefixes = ("apply_form.", "negotiations.", "selectors.LOGIN")
    allowed_statuses = {
        "reference_binding",
        "intentionally_local",
        "not_implemented_upstream",
        "needs_live_evidence",
    }
    required_fields = {
        "coverage_status",
        "origin",
        "verification",
        "evidence",
        "last_verified_at",
        "verified_flow",
        "verified_by",
    }

    audited = {
        logical_id: row
        for logical_id, row in catalog["selectors"].items()
        if logical_id.startswith(prefixes)
    }

    assert audited
    for logical_id, row in audited.items():
        assert required_fields <= row.keys(), logical_id
        assert row["coverage_status"] in allowed_statuses, logical_id
        assert row["origin"] != "llm_hypothesis", logical_id
        if row.get("active", True):
            assert row["origin"] and row["verification"], logical_id
        if row["coverage_status"] == "reference_binding":
            assert row.get("bindings") or row.get("sources"), logical_id
        if row["coverage_status"] == "needs_live_evidence":
            assert row["verification"] in {"unverified", "unavailable"}, logical_id


def test_apply_response_upstream_candidates_have_explicit_safe_decisions():
    catalog = contracts.load_catalog()
    candidates = {
        row["value"]: row
        for row in catalog["upstream_consensus"]
        if contracts._is_apply_response_candidate(row["value"])
    }
    expected = {
        '[data-qa="vacancy-response-letter-submit"]',
        '[data-qa="vacancy-response-letter-toggle"]',
        '[data-qa="vacancy-response-link-bottom"]',
        '[data-qa="vacancy-response-link-top"]',
        '[data-qa="vacancy-response-link-view-topic"]',
        '[data-qa="vacancy-response-submit-popup"]',
        '[data-qa="vacancy-serp__vacancy-employer"]',
    }
    assert set(candidates) == expected
    for value, row in candidates.items():
        assert row["decision"] in {"port_exact", "reject", "unavailable"}, value
        assert row["origin"] == "reference_consensus", value
        assert row["verification"] == "contract_tested", value
        assert row["reason"] and row["target"], value
        assert row["evidence"]["source"] == "selectors/reference-map.yaml:upstream_consensus"
        assert row["evidence"]["note"]
        assert row["last_verified_at"] == "2026-08-25"
        assert row["verified_flow"] == "python scripts/selector_contracts.py check"
        assert row["verified_by"] == "human"


def test_negotiation_withdraw_contracts_are_explicitly_unavailable():
    catalog = contracts.load_catalog()
    expected = {
        "negotiations.NEGOTIATION_WITHDRAW",
        "negotiations.NEGOTIATION_WITHDRAW_CONFIRM",
        "negotiations.NEGOTIATION_WITHDRAW_SUCCESS",
    }
    fail_closed = {
        logical_id
        for logical_id, row in catalog["selectors"].items()
        if logical_id.startswith("negotiations.") and row.get("decision") == "unavailable"
    }

    assert fail_closed == expected
    for logical_id in sorted(expected):
        row = catalog["selectors"][logical_id]
        assert row["active"] is False
        assert row["unavailable_reason"]
        assert logical_id not in contracts.render_generated(catalog)


def test_apply_reachable_search_selectors_are_write_critical():
    logical_ids = {
        "search_page.VACANCY_CARD",
        "search_page.VACANCY_CARD_TITLE_LINK",
        "search_page.VACANCY_CARD_COMPANY",
        "search_page.COMPANY_RATING_VALUE",
        "search_page.COMPANY_RATING_REVIEWS_COUNT",
    }
    assert {contracts.classify_criticality(logical_id) for logical_id in logical_ids} == {"write"}


def test_search_and_vacancy_groups_have_audited_provenance():
    catalog = contracts.load_catalog()
    required = {
        "coverage_status",
        "origin",
        "verification",
        "evidence",
        "last_verified_at",
        "verified_flow",
        "verified_by",
    }
    allowed_statuses = {
        "reference_binding",
        "intentionally_local",
        "not_implemented_upstream",
        "needs_live_evidence",
    }
    for logical_id, row in catalog["selectors"].items():
        if not logical_id.startswith(("search_page.", "vacancy_page.")):
            continue
        assert required <= row.keys(), logical_id
        assert row["coverage_status"] in allowed_statuses
        assert row["origin"] != "llm_hypothesis" or not row.get("active", True)


def test_audit_bootstrap_metadata_is_fail_closed_without_evidence():
    metadata = contracts._audit_metadata(
        "vacancy_page.fixture",
        sources={},
        live_matches=[],
        declared_at="src/hhru_bot/selector_groups/vacancy_page.py:1",
        today="2026-08-25",
    )

    assert metadata["coverage_status"] == "needs_live_evidence"
    assert metadata["verification"] == "unverified"
    assert metadata["last_verified_at"] == "2026-08-25"


def test_refresh_invalidates_stale_reference_audit_metadata():
    catalog = {
        "selectors": {
            "search_page.fixture": {
                "declared_at": "src/hhru_bot/selector_groups/search_page.py:1",
                "sources": {},
                "live_matches": [],
                "coverage_status": "reference_binding",
                "origin": "reference_consensus",
                "verification": "contract_tested",
                "evidence": {"source": "old", "note": "old"},
                "last_verified_at": "2026-08-20",
                "verified_flow": "old",
                "verified_by": "ci",
            }
        }
    }

    contracts._reconcile_audit_metadata(catalog)
    row = catalog["selectors"]["search_page.fixture"]

    assert row["coverage_status"] == "needs_live_evidence"
    assert row["origin"] == "manual"
    assert row["verification"] == "unverified"


def test_refresh_invalidates_stale_reference_metadata_outside_audited_groups():
    catalog = {
        "selectors": {
            "resume_position.fixture": {
                "declared_at": "src/hhru_bot/resume_position.py:1",
                "sources": {},
                "live_matches": [],
                "coverage_status": "reference_binding",
                "origin": "reference_consensus",
                "verification": "contract_tested",
                "evidence": {
                    "source": "old",
                    "note": "old",
                    "runtime_authoritative": True,
                },
                "last_verified_at": "2026-08-20",
                "verified_flow": "old",
                "verified_by": "ci",
            }
        }
    }

    contracts._reconcile_audit_metadata(catalog)
    row = catalog["selectors"]["resume_position.fixture"]

    assert row["coverage_status"] == "needs_live_evidence"
    assert row["evidence"]["runtime_authoritative"] is False
    assert not contracts._has_reviewed_runtime_evidence("resume_position.fixture", row)


def test_refresh_does_not_authorize_a_consensus_downgrade():
    catalog = {
        "selectors": {
            "search_page.fixture": {
                "declared_at": "src/hhru_bot/selector_groups/search_page.py:1",
                "sources": {"steev": [{"value": "[data-qa='x']"}]},
                "live_matches": [],
                "coverage_status": "reference_binding",
                "origin": "reference_consensus",
                "verification": "contract_tested",
                "evidence": {
                    "source": "old",
                    "note": "old",
                    "runtime_authoritative": True,
                },
                "last_verified_at": "2026-08-20",
                "verified_flow": "old",
                "verified_by": "ci",
            }
        }
    }

    contracts._reconcile_audit_metadata(catalog)
    row = catalog["selectors"]["search_page.fixture"]

    assert row["origin"] == "reference_single"
    assert row["evidence"]["runtime_authoritative"] is False


def test_unverified_audit_note_cannot_authorize_runtime_activation():
    row = {
        "active": True,
        "decision": "documented_live",
        "evidence": {
            "source": "audit",
            "note": "not live verified",
            "runtime_authoritative": False,
        },
        "verification": "unverified",
    }

    assert not contracts._has_reviewed_runtime_evidence("vacancy_page.fixture", row)
    row["verification"] = "contract_tested"
    assert not contracts._has_reviewed_runtime_evidence("search_page.fixture", row)
    row["evidence"]["runtime_authoritative"] = True
    assert contracts._has_reviewed_runtime_evidence("search_page.fixture", row)
    assert contracts._has_reviewed_runtime_evidence(
        "account_profile.fixture", {"evidence": row["evidence"]}
    )


def test_catalog_load_does_not_require_generated_live_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts, "MAP_PATH", contracts.ROOT / "selectors" / "reference-map.yaml")
    monkeypatch.setattr(contracts, "EVIDENCE_PATH", tmp_path / "live-evidence.json")

    catalog = contracts.load_catalog()

    assert contracts.verify_catalog(catalog) == []


def test_issue_599_baseline_has_twelve_unique_ids():
    baseline = contracts.load_baseline()["scope"]
    assert baseline["literal_mismatches"] == 12
    assert baseline["broken_bindings"] == 0
    assert baseline["overlap"] == 0
    assert baseline["affected_unique"] == 12
    assert len(baseline["affected_unique_ids"]) == 12
    assert len(set(baseline["affected_unique_ids"])) == 12


def test_issue_609_resume_search_rows_have_explicit_evidence_resolution():
    catalog = contracts.load_catalog()
    logical_ids = [
        logical_id
        for logical_id in catalog["selectors"]
        if logical_id.startswith(
            ("apply.", "apply_form.", "resume_", "search_page.", "vacancy_page.")
        )
    ]
    # These are exactly the non-negotiations rows reported by the 2026-08-25
    # reference refresh.  The other domain's three OFF rows belong to #608.
    target_ids = {
        "apply.success.APPLY_SUCCESS_MARKER",
        "apply_form.APPLY_COVER_LETTER_TEXTAREA",
        "apply_form.APPLY_COVER_LETTER_TOGGLE",
        "apply_form.APPLY_COVER_LETTER_TOGGLE_POPUP",
        "apply_form.APPLY_SUBMIT_BUTTON",
        "resume_experience.EXPERIENCE_ADD_BUTTON",
        "resume_page.RESUME_PUBLISH_BUTTON_DATA_QA",
        "search_page.VACANCY_CARD",
        "search_page.VACANCY_CARD_COMPENSATION",
        "search_page.VACANCY_CARD_RESPONSE_BUTTON",
        "search_page.VACANCY_CARD_TITLE_LINK",
        "vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT",
        "vacancy_page.VACANCY_APPLY_BUTTON",
        "vacancy_page.VACANCY_COMPANY_NAME",
        "vacancy_page.VACANCY_RELOCATION_CONFIRM",
        "vacancy_page.VACANCY_RESPONSE_ERROR",
        "vacancy_page.VACANCY_RESPONSE_REJECT_WARNING",
        "vacancy_page.VACANCY_TITLE",
    }
    assert set(logical_ids) >= target_ids
    # #703 re-ran a read-only live check on these two rows specifically (still
    # unavailable/fail-closed — no candidate control was found in the live DOM
    # either) and stamped that with a fresher date/human reviewer than the
    # rest of this #702 snapshot.
    reverified_in_703 = {
        "resume_experience.EXPERIENCE_ADD_BUTTON",
        "resume_page.RESUME_PUBLISH_BUTTON_DATA_QA",
    }
    for logical_id in target_ids:
        row = catalog["selectors"][logical_id]
        assert row["origin"] in {
            "reference_exact",
            "reference_consensus",
            "reference_single",
            "browser_dom",
            "manual",
        }
        assert row["verification"] in {
            "browser_observed",
            "contract_tested",
            "failed",
            "unavailable",
        }
        assert row["evidence"]["source"]
        if row["bindings"]:
            assert row["evidence"].get("references")
        assert row["verified_flow"]
        if logical_id in reverified_in_703:
            assert row["last_verified_at"] == "2026-08-29"
            assert row["verified_by"] == "human"
        else:
            assert row["last_verified_at"] == "2026-08-25"
            assert row["verified_by"] == "codex"

    for logical_id in {
        "apply.success.APPLY_SUCCESS_MARKER",
        "resume_experience.EXPERIENCE_ADD_BUTTON",
        "resume_page.RESUME_PUBLISH_BUTTON_DATA_QA",
        "search_page.VACANCY_CARD_COMPENSATION",
    }:
        row = catalog["selectors"][logical_id]
        assert row["decision"] == "unavailable"
        assert row["active"] is False


def test_refresh_keeps_audited_unavailable_rows_fail_closed(tmp_path, monkeypatch):
    old = "[data-qa='reference-only']"
    reference_root = tmp_path / "references"
    for config in contracts.REFERENCE_CONFIG.values():
        _commit_repository(reference_root / config["directory"], old)
    catalog = {
        "version": 1,
        "policy": {"mode": "manual", "consensus_threshold": 2},
        "references": {},
        "upstream_consensus": [],
        "selectors": {
            "search_page.unavailable_READ": {
                "value": "[data-qa='not-in-references']",
                "active": False,
                "criticality": "read",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "unavailable",
                "sources": {},
                "bindings": {},
                "live_matches": [],
                "origin": "manual",
                "verification": "unavailable",
                "evidence": {"source": "tests/test_selector_contracts.py:1", "note": "no evidence"},
            },
            "search_page.unavailable_consensus_READ": {
                "value": old,
                "active": False,
                "criticality": "read",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "unavailable",
                "sources": {},
                "bindings": {},
                "live_matches": [],
                "origin": "manual",
                "verification": "unavailable",
                "evidence": {"source": "tests/test_selector_contracts.py:1", "note": "no evidence"},
            },
            "search_page.unavailable_live_READ": {
                "value": old,
                "active": False,
                "criticality": "read",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "unavailable",
                "sources": {},
                "bindings": {},
                "live_matches": ["stale-match"],
                "origin": "manual",
                "verification": "failed",
                "evidence": {
                    "source": "tests/test_selector_contracts.py:1",
                    "note": "failed evidence",
                },
            },
        },
    }
    map_path = tmp_path / "reference-map.yaml"
    map_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(contracts, "MAP_PATH", map_path)
    monkeypatch.setattr(contracts, "EXTRA_CONTRACTS", {})
    refreshed = contracts.refresh_catalog(reference_root, "manual")
    for logical_id in (
        "search_page.unavailable_READ",
        "search_page.unavailable_consensus_READ",
        "search_page.unavailable_live_READ",
    ):
        row = refreshed["selectors"][logical_id]
        assert row["decision"] == "unavailable"
        assert row["active"] is False
        assert row["verification"] in {"unavailable", "failed"}

    current = refreshed
    monkeypatch.setattr(contracts, "load_catalog", lambda: copy.deepcopy(current))
    refreshed_again = contracts.refresh_catalog(reference_root, "manual")
    for logical_id in (
        "search_page.unavailable_READ",
        "search_page.unavailable_consensus_READ",
        "search_page.unavailable_live_READ",
    ):
        row = refreshed_again["selectors"][logical_id]
        assert row["decision"] == "unavailable"
        assert row["active"] is False
        assert row["verification"] in {"unavailable", "failed"}


def test_affected_logical_ids_deduplicates_rows_variants_and_overlap():
    catalog = {
        "selectors": {
            "one": {
                "value": "[data-qa='canonical']",
                "bindings": {
                    "steev": [
                        {"value": "[data-qa='drifted']"},
                        {"value": "[data-qa='drifted']"},
                    ]
                },
                "binding_gaps": ["steev:missing-key"],
            },
            "two": {
                "value": "[data-qa='canonical-two']",
                "bindings": {"tgeruzov": [{"value": "[data-qa='drifted-two']"}]},
            },
            "three": {
                "value": "[data-qa='canonical-three']",
                "bindings": {},
                "binding_gaps": ["yamakayama:missing-key"],
            },
        }
    }
    report = contracts.affected_logical_ids(catalog)
    assert report["literal_mismatch_ids"] == ["one", "two"]
    assert report["broken_binding_ids"] == ["one", "three"]
    assert report["overlap_ids"] == ["one"]
    assert report["affected_unique_ids"] == ["one", "three", "two"]
    assert report["affected_unique"] == 3


def test_refresh_bindings_reports_partial_key_loss():
    catalog = {
        "selectors": {
            "fixture": {
                "value": "[data-qa='canonical']",
                "bindings": {"steev": [{"key": "kept"}, {"key": "removed"}]},
                "sources": {},
            }
        }
    }
    indexes = {
        "steev": [
            contracts.SourceSelector(
                "[data-qa='canonical']", "[data-qa='canonical']", "fixture.py", 1, "kept"
            )
        ],
        "tgeruzov": [],
        "yamakayama": [],
    }
    contracts._refresh_bindings(catalog, indexes)
    assert catalog["selectors"]["fixture"]["binding_gaps"] == ["steev:removed"]


def test_refresh_bindings_preserves_a_missing_key_on_later_refresh():
    catalog = {
        "selectors": {
            "fixture": {
                "value": "[data-qa='canonical']",
                "bindings": {"steev": [{"key": "kept"}]},
                "binding_gaps": ["steev:removed"],
                "sources": {},
            }
        }
    }
    indexes = {
        "steev": [
            contracts.SourceSelector(
                "[data-qa='canonical']", "[data-qa='canonical']", "fixture.py", 1, "kept"
            )
        ],
        "tgeruzov": [],
        "yamakayama": [],
    }
    contracts._refresh_bindings(catalog, indexes)
    assert catalog["selectors"]["fixture"]["binding_gaps"] == ["steev:removed"]


def test_upstream_refresh_preserves_explicit_candidate_decision():
    selector = '[data-qa="vacancy-response-submit-popup"]'
    indexes = {
        name: [
            contracts.SourceSelector(
                selector,
                contracts.normalize_selector(selector),
                "fixture.js",
                10,
                f"fixture-{name}",
            )
        ]
        for name in ("steev", "tgeruzov")
    }
    indexes["yamakayama"] = []
    previous = [
        {
            "value": selector,
            "decision": "reject",
            "reason": "duplicate",
            "target": "apply_form.APPLY_SUBMIT_BUTTON",
            "origin": "reference_consensus",
            "verification": "contract_tested",
            "evidence": {"source": "map", "note": "reviewed"},
            "last_verified_at": "2026-08-25",
            "verified_flow": "check",
            "verified_by": "human",
        }
    ]

    refreshed = contracts._upstream_consensus(indexes, previous)

    assert refreshed[0]["decision"] == "reject"
    assert refreshed[0]["target"] == "apply_form.APPLY_SUBMIT_BUTTON"


def test_changed_upstream_candidate_provenance_invalidates_verification():
    selector = '[data-qa="vacancy-response-submit-popup"]'
    previous = {
        "value": selector,
        "references": ["steev", "tgeruzov"],
        "sources": {"steev": [{"file": "old.js", "line": 1}]},
        "decision": "reject",
        "verification": "contract_tested",
        "evidence": {"source": "old", "note": "reviewed"},
        "last_verified_at": "2026-08-25",
        "verified_by": "human",
    }
    catalog = {
        "references": {
            "steev": {"commit": "new-steev"},
            "tgeruzov": {"commit": "new-tgeruzov"},
        },
        "upstream_consensus": [
            {
                "value": selector,
                "references": ["steev", "tgeruzov"],
                "sources": {"steev": [{"file": "new.js", "line": 2}]},
                "decision": "reject",
                "verification": "contract_tested",
                "evidence": {"source": "old", "note": "reviewed"},
                "last_verified_at": "2026-08-25",
                "verified_by": "human",
            }
        ],
    }

    contracts._invalidate_changed_candidate_verification(
        catalog, [previous], {"steev": False, "tgeruzov": True}
    )

    row = catalog["upstream_consensus"][0]
    assert row["decision"] == "reject"
    assert row["verification"] == "unverified"
    assert row["verified_by"] == "ci"
    assert row["evidence"]["reference_commits"] == {
        "steev": "new-steev",
        "tgeruzov": "new-tgeruzov",
    }


def test_bootstrap_refuses_unresolved_apply_response_candidates(monkeypatch, capsys, tmp_path):
    unresolved = {
        "value": '[data-qa="vacancy-response-submit-popup"]',
        "references": ["steev", "tgeruzov"],
        "sources": {},
    }
    monkeypatch.setattr(
        contracts,
        "parse_args",
        lambda: SimpleNamespace(command="bootstrap", reference_root=tmp_path, live_root=tmp_path),
    )
    monkeypatch.setattr(
        contracts,
        "build_map",
        lambda *_args, **_kwargs: (
            {"upstream_consensus": [unresolved], "selectors": {}},
            {},
        ),
    )

    assert contracts.main() == 1
    assert "bootstrap refused" in capsys.readouterr().err


def test_python_source_key_does_not_depend_on_line_number(tmp_path):
    path = tmp_path / "reference.py"
    path.write_text(
        "def locate():\n    selector = \"[data-qa='stable']\"\n    return selector\n",
        encoding="utf-8",
    )
    before = contracts.extract_python_selectors(path, tmp_path)
    path.write_text(
        "# unrelated comment\n\n"
        "def locate():\n    selector = \"[data-qa='stable']\"\n    return selector\n",
        encoding="utf-8",
    )
    after = contracts.extract_python_selectors(path, tmp_path)
    assert before[0].line != after[0].line
    assert before[0].key == after[0].key == "reference.py::module.locate.selector#0::0"


def test_unmanaged_selector_is_rejected_even_inside_selector_group(tmp_path, monkeypatch):
    source_root = tmp_path / "src" / "hhru_bot"
    group = source_root / "selector_groups" / "invented.py"
    group.parent.mkdir(parents=True)
    group.write_text("INVENTED = \"[data-qa='made-up']\"\n", encoding="utf-8")
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    monkeypatch.setattr(contracts, "SOURCE_ROOT", source_root)
    monkeypatch.setattr(contracts, "GENERATED_PATH", group.parent / "_generated.py")
    findings = contracts.unmanaged_selector_literals()
    assert len(findings) == 1
    assert "invented.py:1" in findings[0]


def test_refresh_dry_run_reports_baseline_and_never_writes(monkeypatch, capsys, tmp_path):
    before = contracts.load_catalog()
    after = copy.deepcopy(before)
    monkeypatch.setattr(
        contracts,
        "parse_args",
        lambda: SimpleNamespace(
            command="refresh",
            reference_root=tmp_path,
            mode="manual",
            dry_run=True,
        ),
    )
    monkeypatch.setattr(contracts, "load_catalog", lambda: copy.deepcopy(before))
    monkeypatch.setattr(contracts, "refresh_catalog", lambda *_args, **_kwargs: after)

    def _unexpected_write(*_args, **_kwargs):
        raise AssertionError("dry-run must not call write_catalog")

    monkeypatch.setattr(contracts, "write_catalog", _unexpected_write)
    assert contracts.main() == 0
    output = capsys.readouterr().out
    assert "DRY-RUN" in output
    assert "no files, branches, or PRs were written" in output
    assert "local selector contracts: 242" in output
    assert "Semantic mismatches:" in output


def test_refresh_tracks_source_keys_and_never_auto_updates_write(tmp_path, monkeypatch):
    reference_root = tmp_path / "references"
    old = "[data-qa='old-selector']"
    new = "[data-qa='new-selector']"
    for config in contracts.REFERENCE_CONFIG.values():
        _commit_repository(reference_root / config["directory"], old)

    metadata, indexes = contracts._reference_indexes(reference_root)
    sources = {name: contracts._matching_sources(old, items) for name, items in indexes.items()}
    catalog = {
        "version": 1,
        "policy": {"mode": "manual", "consensus_threshold": 2},
        "references": metadata,
        "upstream_consensus": contracts._upstream_consensus(indexes),
        "selectors": {
            "search_page.fixture_READ": {
                "value": old,
                "active": True,
                "criticality": "read",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "consensus",
                "sources": sources,
                "live_matches": [],
            },
            "resume_page.fixture_WRITE": {
                "value": old,
                "active": True,
                "criticality": "write",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "consensus",
                "sources": sources,
                "live_matches": [],
            },
            "resume_page.fixture_UNVERIFIED_WRITE": {
                "value": old,
                "active": True,
                "criticality": "write",
                "declared_at": "tests/test_selector_contracts.py:1",
                "decision": "live_dom",
                "sources": {},
                "live_matches": ["old-selector"],
                "evidence": {
                    "source": "tests/test_selector_contracts.py:1",
                    "note": "candidate requires a fresh live check",
                },
                "status": "needs-live-evidence",
                "verification": "unverified",
            },
        },
    }
    map_path = tmp_path / "reference-map.yaml"
    evidence_path = tmp_path / "live-evidence.json"
    map_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    evidence_path.write_text(json.dumps({"selectors": {}}), encoding="utf-8")
    monkeypatch.setattr(contracts, "MAP_PATH", map_path)
    monkeypatch.setattr(contracts, "EVIDENCE_PATH", evidence_path)
    monkeypatch.setattr(contracts, "EXTRA_CONTRACTS", {})

    changed = list(contracts.REFERENCE_CONFIG.values())[:2]
    for config in changed:
        _commit_repository(reference_root / config["directory"], new)

    manual = contracts.refresh_catalog(reference_root, "manual")
    assert manual["selectors"]["search_page.fixture_READ"]["decision"] == "drift_pending"
    assert manual["selectors"]["search_page.fixture_READ"]["suggestion"]["value"] == new
    assert manual["selectors"]["resume_page.fixture_WRITE"]["decision"] == "drift_pending"

    automatic = contracts.refresh_catalog(reference_root, "read_auto")
    assert automatic["selectors"]["search_page.fixture_READ"]["value"] == new
    assert automatic["selectors"]["search_page.fixture_READ"]["decision"] == "consensus"
    assert automatic["selectors"]["resume_page.fixture_WRITE"]["value"] == old
    assert automatic["selectors"]["resume_page.fixture_WRITE"]["decision"] == "drift_pending"
    assert automatic["selectors"]["resume_page.fixture_UNVERIFIED_WRITE"]["decision"] == "live_dom"
    assert automatic["selectors"]["resume_page.fixture_UNVERIFIED_WRITE"]["active"] is True


def test_scheduled_read_auto_is_fail_closed_behind_green_check():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["refresh"]["steps"]
    soak_gate = next(step for step in steps if step.get("id") == "selector_soak_gate")
    read_auto = next(step for step in steps if "--mode read_auto" in step.get("run", ""))

    assert soak_gate["if"] == "${{ github.event_name == 'schedule' }}"
    assert soak_gate["run"] == "python scripts/selector_contracts.py check"
    assert soak_gate["continue-on-error"] is True
    assert (
        read_auto["if"]
        == "${{ github.event_name == 'schedule' && steps.selector_soak_gate.outcome == 'success' }}"
    )
