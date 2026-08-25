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
SPEC = importlib.util.spec_from_file_location("selector_contracts", SCRIPT_PATH)
assert SPEC and SPEC.loader
contracts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contracts
SPEC.loader.exec_module(contracts)

pytestmark = pytest.mark.unit


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
    assert "local selector contracts: 199" in output
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
