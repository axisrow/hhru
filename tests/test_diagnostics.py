import json
import sqlite3
from pathlib import Path

import pytest

from hhru_bot.diagnostics import _same_path, build_bundle, redact

pytestmark = pytest.mark.unit


def test_redaction_adversarial():
    s = "Cookie: abc Authorization=token123 phone +7 (999) 123-45-67 mail a@b.example message: private words"
    out = redact(s)
    assert "abc" not in out and "token123" not in out and "+7" not in out
    assert "a@b.example" not in out and "private words" not in out
    assert "sid=abc" not in redact("Cookie: sid=abc; csrftoken=def")
    assert "credential" not in redact("Authorization: Bearer credential")
    assert "live-secret" not in redact('{"token":"live-secret"}')
    assert "21:19:58" in redact("2026-08-25 21:19:58")
    assert "999" not in redact("8 (999) 123-45-67; (999) 123-45-67")
    for value in (
        '{"authorization": "Bearer quoted-secret"}',
        "{'password': 'python-secret'}",
        "api_key=unquoted-secret csrf_token=csrf-secret",
    ):
        out = redact(value)
        assert "secret" not in out


def test_export_is_deterministic_and_dom_allowlist(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "create table command_runs (run_id text, command text, status text, started_at text, finished_at text)"
        )
        c.execute(
            "insert into command_runs values ('r1','probe','failed','2026-08-25T10:00:00','2026-08-25T10:00:02')"
        )
    html = tmp_path / "r1_probe.html"
    html.write_text('<div data-qa="ok" class="secret">email a@b.io</div>')
    (tmp_path / "r1_probe.json").write_text(
        json.dumps({"run_id": "r1", "producer": "test", "artifact": html.name})
    )
    log = tmp_path / "hhru.log"
    log.write_text(
        "2026-08-25 09:59:59 [INFO] old: outside https://old.example/a\n"
        "2026-08-25 10:00:01 [INFO] hhru_bot.probe: vacancy title https://secret.example/a\n"
        "2026-08-25 10:00:03 [INFO] new: outside\n"
    )
    a = build_bundle(db, run_id="r1", log_path=log, dom_dir=tmp_path)
    b = build_bundle(db, run_id="r1", log_path=log, dom_dir=tmp_path)
    assert a == b
    assert "class" not in json.dumps(a) and "a@b.io" not in json.dumps(a)
    assert "aria-label" not in json.dumps(a) and "href" not in json.dumps(a)
    assert a["snapshots"][0]["nodes"][0]["data-qa"] == "ok"
    assert len(a["log_tail"]) == 1
    assert "vacancy" not in json.dumps(a)


def test_log_window_normalizes_sqlite_microseconds(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as c:
        c.execute("create table command_runs (run_id text, started_at text, finished_at text)")
        c.execute(
            "insert into command_runs values ('r1','2026-01-01T00:00:00.500000','2026-01-01T00:00:00.600000')"
        )
    log = tmp_path / "hhru.log"
    log.write_text("2026-01-01 00:00:00 [INFO] hhru_bot.run: safe event\n")
    assert len(build_bundle(db, run_id="r1", log_path=log)["log_tail"]) == 1


def test_dom_requires_producer_metadata_for_selected_run(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as c:
        c.execute("create table command_runs (run_id text, started_at text, finished_at text)")
        c.execute(
            "insert into command_runs values ('wanted','2026-01-01T00:00:00','2026-01-01T00:00:01')"
        )
    (tmp_path / "wanted.html").write_text("<div data-qa='legacy'>")
    (tmp_path / "wrong.html").write_text("<div data-qa='wrong'>")
    (tmp_path / "wrong.json").write_text(json.dumps({"run_id": "other", "artifact": "wrong.html"}))
    assert build_bundle(db, run_id="wanted", dom_dir=tmp_path)["snapshots"] == []


def test_export_path_aliases_are_detected_and_history_is_read_only(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as c:
        c.execute("create table command_runs (run_id text)")
        c.execute("insert into command_runs values ('r1')")
    alias = tmp_path / "history-alias.db"
    alias.symlink_to(db)
    assert _same_path(db, alias)
    before = Path(db).read_bytes()
    build_bundle(alias, run_id="r1")
    assert Path(db).read_bytes() == before
