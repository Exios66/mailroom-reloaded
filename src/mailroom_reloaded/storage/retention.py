"""Span retention: keep classes, pin/unpin, prune and the showcase seed.

Only span data is ever pruned. The ledger is append-only: pin, unpin, policy and
prune decisions are ledger entries, and pruning never deletes or edits a ledger row.

A span is kept when its run is a *showcase* run (always), pinned, or covered by the
keep policy (``MAILROOM_TRACE_KEEP``, overridden by the latest ``policy`` ledger
entry): ``pinned`` (nothing else), ``recent:<N>`` (the N newest runs) or ``all``.
Everything else older than the live window is deleted at startup and daily.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

import structlog

from mailroom_reloaded.settings import Settings, get_settings, normalize_trace_keep
from mailroom_reloaded.storage.ledger import Ledger
from mailroom_reloaded.storage.span_store import SpanStore, default_span_store_path

__all__ = [
    "SHOWCASE_RUN_IDS",
    "KeepPolicy",
    "effective_policy",
    "is_valid_run_id",
    "maintain",
    "parse_policy",
    "pin",
    "pinned_runs",
    "policy_source",
    "prune",
    "pruned_runs",
    "read_pruned_run_ids",
    "seed_showcase",
    "set_policy",
    "unpin",
]

logger = structlog.get_logger(__name__)

#: Runs that are always kept, never pruned and cannot be unpinned.
SHOWCASE_RUN_IDS: tuple[str, ...] = (
    "showcase-clean",
    "showcase-escalation",
    "showcase-parked-failed",
    "showcase-judge-arbiter",
)
POLICY_RUN_ID = "retention"
_MAX_TARGET = 120  # the ledger bounds label payload values to this length
_DAY_NS = 86_400 * 1_000_000_000
_PAGE = 500


@dataclass(frozen=True)
class KeepPolicy:
    """Which runs besides pinned and showcase ones survive a prune."""

    mode: Literal["pinned", "recent", "all"]
    n: int = 0


def parse_policy(value: str | None) -> KeepPolicy:
    """Parse ``pinned`` / ``all`` / ``recent:<N>`` (1-1000); anything else is ``pinned``."""
    text = normalize_trace_keep(value)
    if text == "all":
        return KeepPolicy("all")
    if text.startswith("recent:"):
        return KeepPolicy("recent", int(text.partition(":")[2]))
    return KeepPolicy("pinned")


def _entries(ledger: Ledger, kind: str) -> list[Any]:
    """Every committed entry of ``kind`` in seq order."""
    out: list[Any] = []
    since = 0
    while True:
        page = ledger.entries(kind=kind, since_seq=since, limit=_PAGE)
        out.extend(page)
        if len(page) < _PAGE:
            return out
        since = page[-1].seq


def _policy_override(ledger: Ledger) -> str | None:
    """The latest ``policy`` ledger entry's value, or ``None`` when none was recorded."""
    latest = ledger.entries(kind="policy", descending=True, limit=1)
    if latest and isinstance(latest[0].payload.get("value"), str):
        return latest[0].payload["value"]
    return None


def effective_policy(ledger: Ledger, settings: Settings | None = None) -> KeepPolicy:
    """The latest ``policy`` ledger entry's value, else ``settings.trace_keep``."""
    override = _policy_override(ledger)
    if override is not None:
        return parse_policy(override)
    return parse_policy((settings or get_settings()).trace_keep)


def policy_source(ledger: Ledger) -> Literal["ledger", "env"]:
    """``ledger`` when a ``policy`` entry overrides the ``MAILROOM_TRACE_KEEP`` default."""
    return "env" if _policy_override(ledger) is None else "ledger"


def is_valid_run_id(run_id: str) -> bool:
    """Whether ``pin``/``unpin`` would accept ``run_id``."""
    try:
        _checked_run_id(run_id)
    except ValueError:
        return False
    return True


def pinned_runs(ledger: Ledger) -> set[str]:
    """Runs whose last ``pinned``/``unpinned`` entry is ``pinned`` (showcase runs not included)."""
    events = sorted(
        _entries(ledger, "pinned") + _entries(ledger, "unpinned"), key=lambda e: e.seq
    )
    pinned: set[str] = set()
    for e in events:
        target = e.payload.get("target")
        if not isinstance(target, str):
            continue
        (pinned.add if e.kind == "pinned" else pinned.discard)(target)
    return pinned


def pruned_runs(ledger: Ledger) -> set[str]:
    """Runs that have a ``pruned`` entry (their spans were deleted)."""
    return {
        t
        for e in _entries(ledger, "pruned")
        if isinstance(t := e.payload.get("target"), str)
    }


def read_pruned_run_ids() -> set[str]:
    """Run ids whose spans retention removed, read with a throwaway ledger.

    Shared by the API and the ``mailroom replay`` CLI. Empty when the ledger is unreadable,
    so listings still work without the marker. A process-wide ledger is not used because it
    would install the external anchor hook.
    """
    from mailroom_reloaded.storage.db import get_engine

    try:
        return pruned_runs(Ledger(get_engine()))
    except Exception:
        logger.warning("replay_pruned_lookup_failed", exc_info=True)
        return set()


def _checked_run_id(run_id: str) -> str:
    from mailroom_reloaded.obs.replay.sessions import parse_session_id

    _, key = parse_session_id(f"run:{run_id}")
    if key != run_id or len(key) > _MAX_TARGET:
        raise ValueError("invalid run id")
    return key


def pin(ledger: Ledger, run_id: str, actor: str = "user") -> bool | None:
    """Pin a run so its spans survive pruning. Raises ``ValueError`` for an invalid run id.

    Returns True when queued, None when the run is already pinned (nothing written), False if the ledger refused.
    """
    run_id = _checked_run_id(run_id)
    if run_id in pinned_runs(ledger):
        return None
    return ledger.append("pinned", run_id, payload={"target": run_id, "actor": actor})


def unpin(ledger: Ledger, run_id: str, actor: str = "user") -> bool | None:
    """Unpin a run. Raises ``ValueError`` for an invalid id or a showcase run.

    Returns True when queued, None when the run is not pinned (nothing written), False if the ledger refused.
    """
    run_id = _checked_run_id(run_id)
    if run_id in SHOWCASE_RUN_IDS:
        raise ValueError("showcase runs are always kept")
    if run_id not in pinned_runs(ledger):
        return None
    return ledger.append("unpinned", run_id, payload={"target": run_id, "actor": actor})


def set_policy(ledger: Ledger, value: str, actor: str = "user") -> bool:
    """Record a keep policy that overrides ``MAILROOM_TRACE_KEEP``. Raises ``ValueError`` if invalid."""
    text = str(value).strip().lower()
    if normalize_trace_keep(text) != text:
        raise ValueError("invalid keep policy: use pinned, all or recent:<N> (1-1000)")
    return ledger.append("policy", POLICY_RUN_ID, payload={"value": text, "actor": actor})


def prune(
    store: SpanStore,
    ledger: Ledger,
    *,
    now_ns: int | None = None,
    window_days: int = 3,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Delete unkept spans older than the live window; returns spans deleted per run.

    Spans without a run are counted under ``unscoped`` and get no ledger entry (there is no
    run to attach one to). Runs still open in the ledger are always kept. Each affected run
    gets one ``pruned`` ledger entry (sha256 of its sorted deleted span ids), committed
    *before* its spans are deleted; if the ledger cannot confirm, nothing is deleted. Exactly
    the previewed span ids are deleted, and ledger rows are never touched. Never raises: a
    failure is logged and ``{}`` returned.
    """
    try:
        policy = effective_policy(ledger, settings)
        if policy.mode == "all":
            return {}
        keep = set(SHOWCASE_RUN_IDS) | pinned_runs(ledger) | set(ledger.open_runs())
        if policy.mode == "recent":
            newest = [
                r["run_id"]
                for r in store.list_runs(limit=policy.n + len(keep))
                if r["run_id"] not in keep
            ]
            keep |= set(newest[: policy.n])
        cutoff = (time.time_ns() if now_ns is None else now_ns) - window_days * _DAY_NS
        doomed: dict[str | None, list[str]] = {}
        for run, span_id in store.spans_older_than(cutoff, exclude_runs=keep):
            doomed.setdefault(run, []).append(span_id)
        if not doomed:
            return {}
        for run, ids in doomed.items():
            if run is None:
                continue
            digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
            queued = ledger.append(
                "pruned",
                run,
                payload={
                    "target": run,
                    "digest": digest,
                    "counts": {"spans": len(ids)},
                },
            )
            if not queued:
                logger.warning("span_prune_aborted", reason="ledger_append_failed")
                return {}
        if not ledger.flush():
            logger.warning("span_prune_aborted", reason="ledger_flush_timeout")
            return {}
        store.delete_spans([i for ids in doomed.values() for i in ids])
        logger.info(
            "spans_pruned", runs=len(doomed), spans=sum(map(len, doomed.values()))
        )
        return {("unscoped" if r is None else r): len(ids) for r, ids in doomed.items()}
    except Exception:
        logger.warning("span_prune_failed", exc_info=True)
        return {}


_SHOWCASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json")


def _read_json(base: Traversable | Path, name: str) -> tuple[bytes, Any]:
    if not _SHOWCASE_NAME.fullmatch(name) or ".." in name:
        raise ValueError(f"bad showcase file name: {name!r}")
    raw = (base / name).read_bytes()
    return raw, json.loads(raw)


def seed_showcase(
    store: SpanStore,
    ledger: Ledger | None = None,
    *,
    base: Traversable | Path | None = None,
) -> list[str]:
    """Import the packaged showcase runs; returns the run ids imported or already present.

    Each file's sha256 must match ``manifest.json``; a mismatch or unreadable file is skipped
    with a warning. A run that already has spans is left alone (idempotent). ``ledger`` is
    accepted for call symmetry and unused: the seed writes spans only. ``base`` overrides the
    ``mailroom_reloaded.showcase`` package resource directory (tests).
    """
    base = (
        base if base is not None else resources.files("mailroom_reloaded") / "showcase"
    )
    try:
        _, manifest = _read_json(base, "manifest.json")
        files = dict(manifest["files"])
    except Exception:
        logger.warning("showcase_manifest_unavailable", exc_info=True)
        return []
    done: list[str] = []
    for name, expected in files.items():
        try:
            raw, doc = _read_json(base, name)
            if hashlib.sha256(raw).hexdigest() != expected:
                logger.warning("showcase_digest_mismatch", file=name)
                continue
            run_id = doc["run_id"]
            if run_id not in SHOWCASE_RUN_IDS:
                logger.warning("showcase_unknown_run", file=name)
                continue
            if store.count(run_id) == 0:
                store.write(
                    [
                        {
                            **row,
                            "attrs": json.dumps(row["attrs"], sort_keys=True),
                            "events": json.dumps(row["events"], sort_keys=True),
                        }
                        for row in doc["spans"]
                    ]
                )
            done.append(run_id)
        except Exception:
            logger.warning("showcase_import_failed", file=name, exc_info=True)
    return done


def maintain(store: SpanStore | None = None, ledger: Ledger | None = None) -> None:
    """Seed the showcase runs, then prune. Best effort: logs and swallows everything."""
    try:
        if store is None:
            store = SpanStore(default_span_store_path())
        if ledger is None:
            from mailroom_reloaded.storage.ledger import get_ledger

            ledger = get_ledger()
        seed_showcase(store, ledger)
        prune(store, ledger)
    except Exception:
        logger.warning("retention_maintain_failed", exc_info=True)
