"""Seed one replayable eval run into the dev span store (``scripts/tui_dev.sh``).

Run ``RUN_ID`` holds six documents: two clean ones, one that retries, one that fails, one
parked for human review and one escalated to the boss. The rows come from the replay
fixture builders in ``tests/fixtures/replay``, re-id'd and stripped to content-free
attributes by the showcase generator. The run is pinned, so the startup retention prune
keeps it. Running again writes nothing new.

Needs ``MAILROOM_BASE_DIR`` (``scripts/tui_dev.sh`` sets it); open the printed path on the API.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.replay import make_showcase as S
from fixtures.replay import span_rows as F

from mailroom_reloaded.storage import retention
from mailroom_reloaded.storage.ledger import Ledger, get_ledger
from mailroom_reloaded.storage.span_store import (
    SpanStore,
    default_span_store_path,
)

RUN_ID = "7e57d0c0ffee"
SESSION = f"{RUN_ID}-session"
SLUG = "devseed"
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
#: One builder per document, in time order.
DOCS = [F.happy, F.retry, F.failed, F.parked, F.boss, F.happy]


def rows() -> list[dict]:
    """The run's span rows, encoded for ``SpanStore.write``."""
    spans: list[dict] = []
    base = int(START.timestamp() * 1e9)
    for n, builder in enumerate(DOCS, 1):
        spans += S._instance(
            builder(),
            RUN_ID,
            SESSION,
            n,
            SLUG,
            base + (n - 1) * 25 * 10**9,
            1.0 + 0.15 * (n % 3),
            S.DOC_TYPES[(n - 1) % len(S.DOC_TYPES)],
        )
    spans.sort(key=lambda s: (s["start_ns"], s["span_id"]))
    return [
        {
            **s,
            "attrs": json.dumps(s["attrs"], sort_keys=True),
            "events": json.dumps(s["events"], sort_keys=True),
        }
        for s in spans
    ]


def seed(store: SpanStore, ledger: Ledger) -> int:
    """Store the run once and pin it. Returns the span rows written (0 when already present)."""
    written = store.write(rows()) if store.count(RUN_ID) == 0 else 0
    if retention.pin(ledger, RUN_ID, actor="tui_dev") is False or not ledger.flush():
        raise RuntimeError("ledger refused the replay pin")
    return written


def main() -> int:
    if not os.environ.get("MAILROOM_BASE_DIR"):
        print(
            "seed_replay: set MAILROOM_BASE_DIR (scripts/tui_dev.sh does); "
            "refusing to write the default or configured span store",
            file=sys.stderr,
        )
        return 2
    store = SpanStore(default_span_store_path())
    try:
        written = seed(store, get_ledger(anchor=False))
    finally:
        store.close()
    state = f"{written} spans written" if written else "already seeded"
    print(f"replay run {RUN_ID}: {state}")
    print(f"/tui#replay=run:{RUN_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
