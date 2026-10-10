#!/usr/bin/env python3
"""Seed two synthetic ``eval_docs`` rows so the /ui "Eval runs" table lists a run.

The replay seed in ``scripts/tui_dev.sh`` writes spans only, so ``/ui`` shows
"no eval runs yet" and none of the ``replay ↗`` / ``grafana ↗`` / ``phoenix ↗`` links
render. ``scripts/demo_capture.mjs`` needs them for the /ui screenshot.

    MAILROOM_BASE_DIR=data/tui-dev/base python3 scripts/demo_seed_eval_runs.py

Stdlib only; idempotent (INSERT OR REPLACE). Touches only the dev SQLite database.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

RUN_ID = "7e57d0c0ffee"  # the replay-seed run (``run:7e57d0c0ffee``)
DDL = (
    "CREATE TABLE IF NOT EXISTS eval_docs (run_id TEXT NOT NULL, filename TEXT NOT NULL,"
    " doc_id TEXT, content_sha256 TEXT, mode TEXT, status TEXT, doc_type TEXT,"
    " doc_subclass TEXT, sort_confidence REAL, sort_mode TEXT, extract_confidence REAL,"
    " schema_valid INTEGER, parse_error TEXT, error_kind TEXT, length_capped INTEGER,"
    " latency_s REAL, prompt_tokens INTEGER, completion_tokens INTEGER, calls INTEGER,"
    " graded INTEGER, judge_overall REAL, judge_fields TEXT, judge_classification TEXT,"
    " gate_features TEXT, route_trail TEXT, PRIMARY KEY (run_id, filename))"
)


def main() -> int:
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "data/tui-dev/base"))
    db = base / "mailroom.db"
    if not db.exists():
        print(f"demo_seed_eval_runs: {db} not found (run scripts/tui_dev.sh up first)", file=sys.stderr)
        return 1
    con = sqlite3.connect(db)
    try:
        con.execute(DDL)
        for name in ("demo-letter.txt", "demo-notice.txt"):
            con.execute(
                "INSERT OR REPLACE INTO eval_docs (run_id, filename, doc_id, status, doc_type, graded)"
                " VALUES (?, ?, ?, 'archived', 'correspondence', 1)",
                (RUN_ID, name, "demo-" + name.split(".")[0]),
            )
        con.commit()
    finally:
        con.close()
    print(f"demo_seed_eval_runs: run {RUN_ID} seeded in {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
