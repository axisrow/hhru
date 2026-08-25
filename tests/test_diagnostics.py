import json
import sqlite3

import pytest

from hhru_bot.diagnostics import build_bundle, redact

pytestmark = pytest.mark.unit


def test_redaction_adversarial():
    s = "Cookie: abc Authorization=token123 phone +7 (999) 123-45-67 mail a@b.example message: private words"
    out = redact(s)
    assert "abc" not in out and "token123" not in out and "+7" not in out
    assert "a@b.example" not in out and "private words" not in out
    assert "sid=abc" not in redact("Cookie: sid=abc; csrftoken=def")
    assert "credential" not in redact("Authorization: Bearer credential")


def test_export_is_deterministic_and_dom_allowlist(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as c:
        c.execute("create table command_runs (run_id text, command text, status text)")
        c.execute("insert into command_runs values ('r1','probe','failed')")
    (tmp_path / "x.html").write_text('<div data-qa="ok" class="secret">email a@b.io</div>')
    a = build_bundle(db, run_id="r1", log_path=tmp_path / "missing", dom_dir=tmp_path)
    b = build_bundle(db, run_id="r1", log_path=tmp_path / "missing", dom_dir=tmp_path)
    assert a == b
    assert "class" not in json.dumps(a) and "a@b.io" not in json.dumps(a)
    assert "aria-label" not in json.dumps(a) and "href" not in json.dumps(a)
    assert a["snapshots"][0]["nodes"][0]["data-qa"] == "ok"
