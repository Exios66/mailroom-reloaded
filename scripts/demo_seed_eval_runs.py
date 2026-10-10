#!/usr/bin/env python3
"""Seed two synthetic ``eval_docs`` rows so the /ui "Eval runs" table lists a run.

The replay seed in ``scripts/tui_dev.sh`` writes spans only, so ``/ui`` shows
"no eval runs yet" and none of the ``replay ↗`` / ``grafana ↗`` / ``phoenix ↗`` links
render. ``scripts/demo_capture.mjs`` needs them for the /ui screenshot.

    MAILROOM_BASE_DIR=data/tui-dev/base uv run python scripts/demo_seed_eval_runs.py

Uses the eval runner's table setup; idempotent (INSERT OR REPLACE). Touches only the dev SQLite database.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from mailroom_reloaded.eval.runner import _ensure_table
from mailroom_reloaded.storage.db import init_db

RUN_ID = "7e57d0c0ffee"  # the replay-seed run (``run:7e57d0c0ffee``)


def main() -> int:
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "data/tui-dev/base"))
    db = base / "mailroom.db"
    # The real DDL (and column migrations) come from the eval runner, so they cannot drift.
    engine = init_db(db)
    _ensure_table(engine)
    engine.dispose()
    con = sqlite3.connect(db)
    try:
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
