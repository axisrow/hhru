#!/usr/bin/env python3
"""Repair the eight #661 competitor salary currencies, fail-closed by default."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from hhru_bot.competitors import CompetitorResume, CompetitorSkill

REPAIRS = {
    "112fbfc80003902b3d0039ed1f4c5170536a61": "BYN",
    "32e3a0e400070ed6ac0039ed1f6b4d70636431": "BYN",
    "82c3d44e0006fbdf1e0039ed1f7676626e7878": "UZS",
    "97dac38d00072f95390039ed1f634467645841": "BYN",
    "9b3d51770002e372430039ed1f6464476f7859": "BYN",
    "b5ea24f00002f3819b0039ed1f315a71756469": "KGS",
    "b7d30d2e0002904b940039ed1f776a6a4b7538": "BYN",
    "daae03dd0002476d470039ed1f376f47345933": "BYN",
}
JSON_FIELDS = (
    "specializations",
    "employment_types",
    "work_formats",
    "languages",
    "education",
)


def _snapshot(conn: sqlite3.Connection, row: sqlite3.Row) -> CompetitorResume:
    values = dict(row)
    skills = [
        CompetitorSkill(skill["skill"], skill["proficiency"])
        for skill in conn.execute(
            "SELECT skill, proficiency FROM competitor_resume_skills "
            "WHERE resume_id = ? ORDER BY rowid",
            (values["resume_id"],),
        )
    ]
    return CompetitorResume(
        resume_id=values["resume_id"],
        resume_url=values["resume_url"],
        desired_role=values["desired_role"],
        salary_from=values["salary_from"],
        salary_to=values["salary_to"],
        salary_currency=values["salary_currency"],
        experience_months=values["experience_months"],
        specializations=json.loads(values["specializations"] or "[]"),
        employment_types=json.loads(values["employment_types"] or "[]"),
        work_formats=json.loads(values["work_formats"] or "[]"),
        skills=skills,
        languages=json.loads(values["languages"] or "[]"),
        education=json.loads(values["education"] or "[]"),
        experience_summary=values["experience_summary"],
        achievements=values["achievements"],
    )


def _hash_mismatches(conn: sqlite3.Connection) -> int:
    return sum(
        snapshot.content_hash() != row["content_hash"]
        for row in conn.execute("SELECT * FROM competitor_resumes")
        for snapshot in [_snapshot(conn, row)]
    )


def repair(db: Path, *, apply: bool) -> None:
    if not db.is_file():
        raise SystemExit(f"database not found: {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            row["resume_id"]: row
            for row in conn.execute(
                "SELECT * FROM competitor_resumes WHERE salary_currency = 'руки'"
            )
        }
        if set(rows) != set(REPAIRS):
            raise SystemExit(
                f"preflight failed: expected exactly {len(REPAIRS)} known rows, "
                f"found {len(rows)} ({sorted(rows)})"
            )
        before = {resume_id: tuple(row) for resume_id, row in rows.items()}
        print(f"preflight: {len(rows)} rows with salary_currency='руки'")
        for resume_id in sorted(REPAIRS):
            row = rows[resume_id]
            print(
                f"before {resume_id} salary={row['salary_from']}-{row['salary_to']} "
                f"currency={row['salary_currency']} -> {REPAIRS[resume_id]}"
            )
        if not apply:
            print("dry-run: no database changes")
            return

        backup = db.with_name(
            f"{db.stem}.pre-661-{datetime.now().strftime('%Y%m%d%H%M%S')}{db.suffix}"
        )
        backup_conn = sqlite3.connect(backup)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for resume_id, currency in REPAIRS.items():
                row = conn.execute(
                    "SELECT * FROM competitor_resumes WHERE resume_id = ?",
                    (resume_id,),
                ).fetchone()
                if row is None or row["salary_currency"] != "руки":
                    raise RuntimeError(f"row changed during preflight: {resume_id}")
                snapshot = _snapshot(conn, row)
                repaired = CompetitorResume(
                    **{
                        **snapshot.__dict__,
                        "salary_currency": currency,
                    }
                )
                cur = conn.execute(
                    "UPDATE competitor_resumes SET salary_currency = ?, content_hash = ? "
                    "WHERE resume_id = ? AND salary_currency = 'руки'",
                    (currency, repaired.content_hash(), resume_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"unexpected update count for {resume_id}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        changed = 0
        for resume_id, old in before.items():
            new = tuple(
                conn.execute(
                    "SELECT * FROM competitor_resumes WHERE resume_id = ?", (resume_id,)
                ).fetchone()
            )
            if any(old[index] != new[index] for index in range(len(old)) if index not in {5, 14}):
                raise RuntimeError(f"unexpected field change for {resume_id}")
            if new[5] != REPAIRS[resume_id]:
                raise RuntimeError(f"unexpected repaired currency for {resume_id}")
            changed += old[5] != new[5] and old[14] != new[14]
            print(f"after  {resume_id} salary={new[3]}-{new[4]} currency={new[5]}")
        if changed != len(REPAIRS) or _hash_mismatches(conn) != 0:
            raise RuntimeError("postflight failed: changed rows or hash mismatches")
        print(f"postflight: changed_rows={changed}, hash_mismatches=0")
        print(f"backup: {backup}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="commit the repair")
    args = parser.parse_args()
    repair(args.db, apply=args.apply)
