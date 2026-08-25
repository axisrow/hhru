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
}
AUDIT_STATUSES = {
    "reference binding",
    "intentionally local",
    "not implemented upstream",
    "needs-live-evidence",
}
AUDIT_FIELDS = (
    "origin",
    "verification",
    "evidence",
    "last_verified_at",
    "verified_flow",
    "verified_by",
)


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


def test_resume_account_competitor_unbound_selectors_have_provenance():
    catalog = contracts.load_catalog()
    rows = {
        logical_id: row
        for logical_id, row in catalog["selectors"].items()
        if logical_id.split(".", 1)[0] in AUDIT_GROUPS
    }

    assert rows
    for logical_id, row in rows.items():
        assert row.get("status") in AUDIT_STATUSES, logical_id
        assert all(row.get(field) not in (None, "", {}) for field in AUDIT_FIELDS), logical_id
        assert "llm_hypothesis" not in row, logical_id
        if not (row.get("bindings") or row.get("sources")):
            assert row["status"] != "reference binding", logical_id


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


def test_catalog_load_does_not_require_generated_live_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts, "MAP_PATH", contracts.ROOT / "selectors" / "reference-map.yaml")
    monkeypatch.setattr(contracts, "EVIDENCE_PATH", tmp_path / "live-evidence.json")

    catalog = contracts.load_catalog()

    assert contracts.verify_catalog(catalog) == []


def test_issue_599_baseline_has_twelve_unique_ids():
    baseline = contracts.load_baseline()["scope"]
    assert baseline["literal_mismatches"] == 10
    assert baseline["broken_bindings"] == 3
    assert baseline["overlap"] == 1
    assert baseline["affected_unique"] == 12
    assert len(baseline["affected_unique_ids"]) == 12
    assert len(set(baseline["affected_unique_ids"])) == 12


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
    assert "local selector contracts: 204" in output
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
