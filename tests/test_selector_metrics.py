"""Runtime selector observations and the diagnostics bundle contract (#701)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from hhru_bot.commands import probe as probe_command
from hhru_bot.diagnostics import build_bundle
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _history_with_run(tmp_path: Path) -> tuple[History, str]:
    history = History(tmp_path / "history.db")
    run_id = history.start_command_run(command="probe", requested_limit=None)
    return history, run_id


def test_bundle_reports_runtime_hits_misses_and_optional_absence(tmp_path):
    history, run_id = _history_with_run(tmp_path)
    pages = [
        probe_command.PageCheck(
            "search",
            "https://hh.ru/search/vacancy?text=python",
            [
                probe_command.SelectorCheck("VACANCY_CARD", "ignored-css", 2),
                probe_command.SelectorCheck("VACANCY_CARD_TITLE_LINK", "ignored-css", 0),
                probe_command.SelectorCheck("PAGINATION_NEXT", "ignored-css", 0, required=False),
            ],
        )
    ]
    history.record_selector_observations(run_id, probe_command._healthcheck_observations(pages))

    selectors = build_bundle(tmp_path / "history.db", run_id=run_id)["selectors"]
    assert selectors["hits"] == ["search_page.VACANCY_CARD"]
    assert selectors["misses"] == ["search_page.VACANCY_CARD_TITLE_LINK"]
    assert selectors["evidence"][2]["status"] == "OPTIONAL_ABSENT"
    assert "search_page.PAGINATION_NEXT" not in selectors["misses"]


def test_indeterminate_states_recorded_nowhere(tmp_path):
    history, run_id = _history_with_run(tmp_path)
    pages = [
        probe_command.PageCheck("search", "u", [], unreachable=True),
        probe_command.PageCheck("negotiations", "u", [], unauthenticated=True),
        probe_command.PageCheck("resume", "u", [], placeholder=True),
    ]
    history.record_selector_observations(run_id, probe_command._healthcheck_observations(pages))
    assert build_bundle(tmp_path / "history.db", run_id=run_id)["selectors"] == {
        "hits": [],
        "misses": [],
        "evidence": [],
    }


def test_metrics_are_scoped_by_run_id_and_catalog_is_not_a_hit(tmp_path):
    history, wanted = _history_with_run(tmp_path)
    history.finish_command_run(
        wanted,
        status="completed",
        exit_code=0,
        attempted=0,
        success=0,
        failed=0,
        uncertain=0,
        skipped=0,
    )
    other = history.start_command_run(command="probe", requested_limit=None)
    history.record_selector_observations(
        other,
        [
            {
                "logical_id": "search_page.VACANCY_CARD",
                "status": "OK",
                "found": 1,
                "evidence": "runtime observation",
            }
        ],
    )
    assert build_bundle(tmp_path / "history.db", run_id=wanted)["selectors"] == {
        "hits": [],
        "misses": [],
        "evidence": [],
    }


def test_unknown_logical_id_rejected():
    pages = [
        probe_command.PageCheck(
            "search", "u", [probe_command.SelectorCheck("NOT_IN_CATALOG", "css", 1)]
        )
    ]
    with pytest.raises(ValueError, match="unknown selector logical ID"):
        probe_command._healthcheck_observations(pages)


def test_healthcheck_persists_observations_with_command_run(monkeypatch, tmp_path):
    db = tmp_path / "history.db"
    pages = [
        probe_command.PageCheck(
            "search",
            "https://hh.ru/search/vacancy?text=python",
            [probe_command.SelectorCheck("VACANCY_CARD", "ignored-css", 1)],
        )
    ]

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def new_page(self):
            return object()

    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(storage_state_file="session.json"),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_kw: _Context())
    monkeypatch.setattr(probe_command, "_healthcheck_spec", lambda _config: [("search", "u", [])])
    monkeypatch.setattr(probe_command, "check_selectors", lambda *_a, **_kw: pages)

    failed = probe_command.run_healthcheck(
        SimpleNamespace(config="config.yaml", headless=True, json=False, history=str(db))
    )

    assert failed is False
    bundle = build_bundle(db)
    assert bundle["selectors"]["hits"] == ["search_page.VACANCY_CARD"]
    assert bundle["run"]["command"] == "probe"


def test_healthcheck_records_interrupted_run(monkeypatch, tmp_path):
    db = tmp_path / "history.db"

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def new_page(self):
            return object()

    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(storage_state_file="session.json"),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_kw: _Context())
    monkeypatch.setattr(probe_command, "_healthcheck_spec", lambda _config: [("search", "u", [])])

    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(probe_command, "check_selectors", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        probe_command.run_healthcheck(
            SimpleNamespace(config="config.yaml", headless=True, json=False, history=str(db))
        )

    run = History(db).command_runs()[0]
    assert run["status"] == "interrupted"
    assert run["exit_code"] == 130
    assert run["detail"] == "SIGINT"


def test_metrics_are_redacted(tmp_path):
    history, run_id = _history_with_run(tmp_path)
    history.record_selector_observations(
        run_id,
        [
            {
                "logical_id": "search_page.VACANCY_CARD",
                "status": "OK",
                "found": 1,
                "evidence": 'token="runtime-secret" email=a@b.example',
            }
        ],
    )
    output = json.dumps(build_bundle(tmp_path / "history.db", run_id=run_id))
    assert "runtime-secret" not in output
    assert "a@b.example" not in output
    assert "[REDACTED]" in output or "[REDACTED_EMAIL]" in output


def test_bundle_matches_json_schema_and_golden(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE command_runs "
            "(run_id TEXT, status TEXT, started_at TEXT, finished_at TEXT)"
        )
        conn.execute(
            "INSERT INTO command_runs VALUES "
            "('fixture', 'failed', '2026-08-25T10:00:00', '2026-08-25T10:00:01')"
        )
    log = tmp_path / "hhru.log"
    log.write_text(
        "2026-08-25 10:00:00 [INFO] hhru_bot.commands: [RUN] started\n",
        encoding="utf-8",
    )

    bundle = build_bundle(db, run_id="fixture", log_path=log)
    schema = json.loads(Path("schemas/diagnostics.schema.json").read_text())
    jsonschema.validate(bundle, schema)

    golden = json.loads(Path("tests/fixtures/diagnostics/golden.json").read_text())
    bundle["environment"] = golden["environment"]
    bundle["run"].pop("started_at")
    bundle["run"].pop("finished_at")
    assert bundle == golden