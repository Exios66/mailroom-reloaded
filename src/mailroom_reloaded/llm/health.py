"""Passive per-provider LLM health for the operator pager (port of llm-mailroom's
`llm/provider_health.py`).

`llm/tooling.chat_create` records every chat-completion attempt here; nothing probes a
provider (a request to a scaled-to-zero Modal tier would cold-start a GPU).
Events live in `<base_dir>/provider_health.json` as
`{key: [[ts, ok, status, latency_s], …]}` (≤200 per key), merged under a
flock so the API, watcher and run scripts can all write. Disabled with
`MAILROOM_PROVIDER_HEALTH=0` (tests). Never raises.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mailroom_reloaded.settings import get_settings

CAP, FLUSH_S = 200, 5.0


def provider_key(base_url: str | None, model: str | None) -> str:
    """Group a base URL under a Modal tier, OpenRouter, or another hostname.

    The configured vLLM URL maps to ``modal:vllm``. ``model`` does not affect
    this grouping.
    """
    url, model = str(base_url or ""), str(model or "")
    vllm = (get_settings().vllm_base_url or "").strip().rstrip("/")
    if vllm and url.rstrip("/") == vllm:
        return "modal:vllm"
    m = re.search(r"--mailroom-vllm-([a-z0-9]+)\.modal\.run", url)
    if m:
        return f"modal:{m[1]}"
    if "openrouter.ai" in url:
        return "openrouter"
    return f"other:{urlparse(url).hostname or 'unknown'}"


def _path() -> Path:
    """Create the configured base directory and return its provider health file path."""
    base = Path(get_settings().base_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / "provider_health.json"


def _load(path: Path) -> dict:
    """Read a JSON object, returning an empty dict for unreadable or invalid content."""
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class _Buffer:
    def __init__(self):
        """Initialize an empty event buffer and per-provider success history."""
        self.events: dict[str, list] = {}
        self.last_ok: dict[str, bool] = {}
        self.last_flush = 0.0
        self.lock = threading.Lock()

    def add(self, key: str, event: list, now: float) -> None:
        """Buffer ``[epoch_seconds, ok, status, latency_seconds]`` for a provider.

        Flush on a transition to failure or when at least ``FLUSH_S`` seconds
        have elapsed. Flush errors propagate to the caller.
        """
        with self.lock:
            self.events.setdefault(key, []).append(event)
            turned_bad = self.last_ok.get(key, True) and not event[1]
            self.last_ok[key] = bool(event[1])
        if turned_bad or now - self.last_flush >= FLUSH_S:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        """Drain buffered events into the shared health file under an exclusive lock.

        Keep the latest ``CAP`` events per provider. ``now`` is epoch seconds,
        defaulting to the current time; filesystem and serialization errors
        propagate after the pending events have been removed from the buffer.
        """
        with self.lock:
            pending, self.events = self.events, {}
            self.last_flush = time.time() if now is None else now
        if not pending:
            return
        path = _path()
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _load(path)
            for key, evs in pending.items():
                data[key] = sorted(data.get(key, []) + evs, key=lambda e: e[0])[-CAP:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)


_BUF = _Buffer()
atexit.register(lambda: _safe(_BUF.flush))


def _safe(fn, *a, **kw):
    """Return the callable result, or None if it raises an Exception."""
    try:
        return fn(*a, **kw)
    except Exception:  # noqa: BLE001 — health logging must never break an LLM call
        return None


def record_llm_outcome(base_url, model, *, ok: bool, status: int | None, latency_s: float | None,
                       now: float | None = None) -> None:
    """Buffer one completion attempt, suppressing recording errors.

    ``status`` is the HTTP status or None for no response; ``latency_s`` is
    elapsed seconds and ``now`` is epoch seconds, defaulting to the current
    time. ``MAILROOM_PROVIDER_HEALTH=0`` disables recording.
    """
    if os.environ.get("MAILROOM_PROVIDER_HEALTH", "1") == "0":
        return
    now = time.time() if now is None else now
    _safe(lambda: _BUF.add(provider_key(base_url, model), [now, bool(ok), status, latency_s], now))


def _outage(e: list, key: str = "") -> bool:
    """A failure that says the provider is unhealthy: no response, 5xx, 401/403.

    A Modal 503 is a cold start, not an outage (any other provider's 503 is). Rate limits (429)
    and request errors (400/404/422) are the caller's problem, not an outage."""
    status = e[2]
    cold_start = status == 503 and key.startswith("modal:")
    return not e[1] and (status is None or status in (401, 403) or (status >= 500 and not cold_start))


def read_provider_health(now: float | None = None, window_s: float = 600) -> dict[str, dict]:
    """Summarize persisted and buffered attempts within ``window_s`` seconds.

    ``now`` is epoch seconds, defaulting to the current time. Providers with
    no events at or after ``now - window_s`` are omitted. Each summary has
    call and outage counts, a failure streak since the last success, the
    last success timestamp, a 503 count, and median latency in seconds.
    Non-outage failures neither extend nor reset the streak.

    Unreadable or invalid JSON is treated as empty history. Errors creating
    the base directory and errors in malformed event data can propagate.
    """
    now = time.time() if now is None else now
    data = _safe(_load, _path()) or {}
    with _BUF.lock:
        for key, evs in _BUF.events.items():
            data[key] = data.get(key, []) + evs
    out = {}
    for key, evs in data.items():
        evs = sorted((e for e in evs if isinstance(e, list) and len(e) == 4), key=lambda e: e[0])
        recent = [e for e in evs if now - e[0] <= window_s]
        if not recent:
            continue
        streak = 0
        for e in reversed(recent):
            if e[1]:
                break
            streak += _outage(e, key)  # 429/4xx/Modal 503 neither break nor extend an outage streak
        lats = [e[3] for e in recent if e[3] is not None]
        last_bad = next((e[2] for e in reversed(recent) if _outage(e, key)), None)
        out[key] = {"calls": len(recent), "errors": sum(1 for e in recent if _outage(e, key)),
                    "ok_calls": sum(1 for e in recent if e[1]),
                    "consecutive_errors": streak, "last_status": last_bad if streak else recent[-1][2],
                    "last_ok_at": next((e[0] for e in reversed(evs) if e[1]), None),
                    "count_503": sum(1 for e in recent if e[2] == 503),
                    "p50_latency_s": statistics.median(lats) if lats else None}
    return out
