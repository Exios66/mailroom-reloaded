"""Operator SMS pager host: ledger-backed snapshot, scheduler, settings, CLI.

The pure decisions (rules, frequency state machine, SMS copy, JSON payload)
live in ``obs/pager_core.py`` (verbatim from llm-mailroom); this module
collects a ``RunSnapshot`` from the archive ledger, the bins, the watcher lock,
passive provider health, Modal and OpenRouter, and sends through ``obs/sms``.
CLI: ``mailroom pager <verb>`` (see ``main``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

from mailroom_reloaded.obs.pager_core import (
    PagerConfig,
    ProviderStatus,
    RunProgress,
    RunSnapshot,
)
from mailroom_reloaded.settings import get_settings

logger = structlog.get_logger(__name__)


def get_base_dir() -> Path:
    """Return the configured data directory, creating it if necessary."""
    base = Path(get_settings().base_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write(path: Path, data) -> None:
    """Replace a JSON file via a temporary file, creating parent directories as needed.

    Serialization and filesystem errors propagate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")  # CLI and pager never share
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# ── providers ────────────────────────────────────────────────────────

_OR_CACHE: dict = {}


def openrouter_info(cfg: PagerConfig, client: httpx.Client | None = None) -> dict | None:
    """Return cached OpenRouter ``GET /key`` status, or None without a usable key.

    Cache for ``providers.openrouter_cache_minutes`` minutes. HTTP failures,
    request exceptions, and response parsing errors become cached dicts
    with an ``error`` field.
    """
    key = (get_settings().openrouter_api_key or "").strip()
    if not key or key == "mock-key":
        return None
    if _OR_CACHE and time.time() - _OR_CACHE["at"] < cfg.providers.openrouter_cache_minutes * 60:
        return _OR_CACHE["info"]
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    u = urlparse(base)
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
        info = {"error": "insecure base URL"}  # never send the API key in cleartext
        _OR_CACHE.update(at=time.time(), info=info)
        return info
    try:
        c = client or httpx.Client(timeout=5.0)
        r = c.get(f"{base}/key", headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
        if r.status_code != 200:
            info = {"error": f"HTTP {r.status_code}"}
        else:
            d = r.json().get("data") or {}
            info = {k: d.get(k) for k in ("limit_remaining", "usage", "is_free_tier")}
    except Exception as exc:  # noqa: BLE001 — a status probe never raises into the pager
        info = {"error": type(exc).__name__}
    _OR_CACHE.update(at=time.time(), info=info)
    return info


def modal_warm(app: str = "mailroom-vllm", run=subprocess.run) -> dict[str, int] | None:
    """Warm containers of the vLLM app via the read-only ``modal container list --json`` (never wakes a GPU)."""
    if not shutil.which("modal"):
        return None
    try:
        r = run(["modal", "container", "list", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        n = sum(1 for c in json.loads(r.stdout or "[]") if str(c.get("App Name") or c.get("app_name") or "") == app)
        return {"vllm": n}
    except Exception:  # noqa: BLE001
        return None


def _modal_tiers() -> list[str]:
    """Return the vLLM tier when it is the configured provider, otherwise no tiers."""
    return ["vllm"] if get_settings().provider == "vllm" else []


def provider_statuses(cfg: PagerConfig, health: dict, or_info: dict | None, warm: dict | None) -> list[ProviderStatus]:
    """Combine passive health, Modal container counts, and key status into provider states.

    Include the configured vLLM tier and Modal tiers present in ``health``.
    Three consecutive outages mark a provider down. Modal degradation also
    uses configured error thresholds or repeated 503s without a success.
    Omit OpenRouter when neither key status nor traffic indicates its use.
    """
    rd, warm = cfg.rules["provider_degraded"], warm or {}
    tiers = list(dict.fromkeys(_modal_tiers() + [k[6:] for k in health if k.startswith("modal:")]))
    out = []
    for t in tiers:
        h = health.get(f"modal:{t}") or {}
        calls, errs = h.get("calls", 0), h.get("errors", 0)
        if h.get("consecutive_errors", 0) >= 3:
            st, det = "down", f"HTTP {h.get('last_status')}" if h.get("last_status") else "no response"
        elif calls >= rd["min_calls"] and errs / calls >= rd["error_rate"]:
            st, det = "degraded", ""
        elif h.get("count_503", 0) >= rd["min_calls"] and not h.get("ok_calls"):
            st, det = "degraded", f"cold start: {h['count_503']}× 503, no success yet"
        elif h.get("count_503"):
            st, det = "up", f"cold start: {h['count_503']}× 503"
        elif not calls:
            st, det = ("warm", _n_containers(warm[t])) if warm.get(t) else ("idle", "")
        else:
            st, det = "up", ""
        out.append(ProviderStatus(f"modal:{t}", st, det, calls, errs))
    h = health.get("openrouter") or {}
    info = or_info or {}
    if "error" in info and info["error"] in ("HTTP 401", "HTTP 403"):
        st, det = "down", info["error"]
    elif h.get("consecutive_errors", 0) >= 3:
        st, det = "down", f"HTTP {h.get('last_status')}" if h.get("last_status") else "no response"
    elif or_info and "error" not in info:
        left = info.get("limit_remaining")
        st, det = "up", f"${left:.2f} left" if left is not None else "no limit"
    elif h.get("calls"):
        st, det = "up", ""
    else:
        return out  # no key and no traffic: OpenRouter is not in use here
    out.append(ProviderStatus("openrouter", st, det, h.get("calls", 0), h.get("errors", 0)))
    return out


def _n_containers(n: int) -> str:
    """Format a container count with singular or plural wording."""
    return f"{n} container{'' if n == 1 else 's'}"


# ── snapshot (archive ledger) ─────────────────────────────────────────


def _source(name: str, fn, default):
    """Return a snapshot source result, or the supplied default if it raises an Exception."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one dead source never blanks the digest
        logger.warning("pager_snapshot_source_failed", source=name, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return default


def _ts(entry) -> float:
    """Convert a ledger entry ISO timestamp to epoch seconds; invalid timestamps raise ValueError."""
    return datetime.fromisoformat(str(entry.ts)).timestamp()


def _live_run_ids(since: float, now: float) -> list[str]:
    """Live run ids covering ``[since, now]``: one per UTC date (ids roll at UTC midnight)."""
    from mailroom_reloaded.obs.run_context import live_run_id

    day = datetime.fromtimestamp(since, UTC).date()
    ids = []
    while day <= datetime.fromtimestamp(now, UTC).date():
        ids.append(live_run_id(datetime(day.year, day.month, day.day, tzinfo=UTC)))
        day += timedelta(days=1)
    return list(dict.fromkeys(ids))


def _closed_since(ledger, since: float, run_ids: list[str] | None) -> list:
    """``doc_closed`` entries at or after ``since`` (newest first, paged); ``None`` means every run."""
    out = []
    for run_id in run_ids or [None]:
        offset = 0
        while True:
            page = ledger.entries(kind="doc_closed", run_id=run_id, limit=500, offset=offset, descending=True)
            fresh = [e for e in page if _ts(e) >= since]
            out += fresh
            if len(fresh) < len(page) or len(page) < 500:
                break
            offset += 500
    return sorted(out, key=_ts, reverse=True)


def _doc_cost(e) -> float:
    """Sum document usage costs across roles in USD, treating absent costs as zero."""
    return sum(float((u or {}).get("cost_usd") or 0) for u in (e.payload.get("usage_by_role") or {}).values())


def window_stats(entries: list) -> dict:
    """Summarize terminal ledger entries with USD spend and median latency in seconds.

    ``received`` counts closed documents; parked outcomes count as review,
    and failed or aborted outcomes count as failures. Zero or missing
    latencies are excluded; an empty latency sample yields None.
    ``first_pass_rate`` is unavailable and always None.
    """
    outs = [e.payload.get("outcome") for e in entries]
    lats = [float(e.payload["duration_s"]) for e in entries if e.payload.get("duration_s")]
    return {"received": len(entries), "archived": outs.count("completed"), "review": outs.count("parked"),
            "failed": outs.count("failed") + outs.count("aborted"),
            "cost_usd": round(sum(_doc_cost(e) for e in entries), 4),
            "p50_latency_s": statistics.median(lats) if lats else None, "first_pass_rate": None}


def _eval_score(run_id: str) -> float | None:
    """Return the mean non-null evaluation score, or None if absent; database errors propagate."""
    from sqlalchemy import text

    from mailroom_reloaded.eval.runner import _engine

    with _engine().connect() as conn:
        v = conn.execute(text("SELECT AVG(overall_score) FROM eval_docs WHERE run_id = :r AND overall_score IS NOT NULL"),
                         {"r": run_id}).scalar()
    return None if v is None else float(v)


def eval_runs(ledger, now: float, max_age_s: float = 86400) -> list[RunProgress]:
    """``eval`` runs as research progress: open ones, plus ones closed within ``max_age_s`` (finished).

    done = documents closed, total from ``run_opened``."""
    ids = [(r, None) for r in ledger.open_runs(kind="eval")]
    for c in ledger.entries(kind="run_closed", limit=50, descending=True):
        if 0 <= now - _ts(c) <= max_age_s:
            ids.append((c.run_id, c))
    out = []
    for run_id, closed_entry in ids:
        opened = (ledger.entries(run_id=run_id, kind="run_opened", limit=1) or [None])[0]
        if opened is None or opened.payload.get("kind") != "eval":
            continue
        p = opened.payload
        closed = ledger.entries(run_id=run_id, kind="doc_closed", limit=1, descending=True)
        failed = sum(1 for e in ledger.entries(run_id=run_id, kind="doc_closed", limit=5000)
                     if e.payload.get("outcome") in ("failed", "aborted"))
        metrics = {"failed": failed}
        score = _source("eval_score", lambda r=run_id: _eval_score(r), None)
        if score is not None:
            metrics["acc"] = score
        out.append(RunProgress(run_id, f"eval {p.get('posture_label') or p.get('mode') or ''}".strip(),
                               int(p.get("total") or 0), ledger.count("doc_closed", run_id), _ts(opened),
                               _ts(closed_entry) if closed_entry else _ts(closed[0]) if closed else _ts(opened),
                               closed_entry is not None, metrics))
    return out


def watcher_running(base: Path) -> bool | None:
    """Return whether ``watcher.lock`` exists and cannot be exclusively locked.

    A missing or free lock returns False. Lock acquisition errors return
    True; errors opening the lock file propagate.
    """
    import fcntl

    path = base / "watcher.lock"
    if not path.exists():
        return False
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


def _local_midnight(now: float, tz: str) -> datetime:
    """Return midnight for the date containing epoch seconds ``now`` in ``tz``.

    A blank timezone uses the host timezone. Timezone lookup errors propagate.
    """
    from zoneinfo import ZoneInfo

    d = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now).astimezone()
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _git_sha() -> str | None:
    """Return the short Git revision, or None for empty output or subprocess errors."""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                           cwd=Path(__file__).resolve().parent, check=False)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _count(d: Path) -> int:
    """Count immediate, nonhidden files excluding names ending in ``.meta``."""
    return sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".meta"))


def collect_snapshot(cfg: PagerConfig, *, window_minutes: float, now: float | None = None) -> RunSnapshot:
    """Collect ledger, queue, watcher, and provider data for the pager.

    ``now`` is epoch seconds, defaulting to the current time;
    ``window_minutes`` selects the trailing ledger window. Production mode
    restricts ledger statistics to live run IDs spanning that window or the
    local day; research mode includes all runs. Individual source failures
    use empty or unknown values. Base directory, timezone, and ledger data
    conversion errors outside those source guards can propagate.
    """
    from mailroom_reloaded.llm.health import read_provider_health
    from mailroom_reloaded.storage.bins import Bins
    from mailroom_reloaded.storage.ledger import get_ledger

    now = time.time() if now is None else now
    base, bins = get_base_dir(), Bins(get_base_dir())
    ledger = _source("ledger", get_ledger, None)
    def runs(since: float) -> list[str] | None:
        """Select live run IDs since epoch seconds ``since``, or all runs in research mode."""
        return _live_run_ids(since, now) if cfg.run_mode == "production" else None
    queues = {"inbox": _source("inbox", lambda: _count(bins.inbox), 0),
              "processing": _source("processing", lambda: sum(
                  1 for p in (base / "processing").rglob("*") if p.is_file()) if (base / "processing").exists() else 0, 0),
              "review": _source("review", lambda: _count(bins.review), 0),
              "failed": _source("failed", lambda: _count(bins.failed), 0)}
    since = now - window_minutes * 60
    recent = _source("ledger_window", lambda: _closed_since(ledger, since, runs(since)), [])
    window = {"minutes": window_minutes, **window_stats(recent)} if ledger is not None else {}
    midnight = _local_midnight(now, cfg.timezone).timestamp()
    today_entries = _source("ledger_today", lambda: _closed_since(ledger, midnight, runs(midnight)), None)
    t = window_stats(today_entries) if today_entries is not None else None
    today = {"processed": t["received"], "review": t["review"], "failed": t["failed"],
             "cost_usd": t["cost_usd"]} if t else None
    fails = [e for e in recent if e.payload.get("outcome") in ("failed", "aborted")][:3]
    health = _source("provider_health", lambda: read_provider_health(now), {})
    or_info = _source("openrouter", lambda: openrouter_info(cfg), None)
    warm = _source("modal", modal_warm, None)
    return RunSnapshot(
        taken_at=now, mode=cfg.run_mode, watcher_alive=_source("watcher", lambda: watcher_running(base), None),
        heartbeat_age_s=None, uptime_s=None, sha=_git_sha(), queues=queues, window=window, stuck=0, paused=None,
        outbox_dead=None, providers=_source("providers", lambda: provider_statuses(cfg, health, or_info, warm), []),
        runs=_source("runs", lambda: eval_runs(ledger, now), []),
        failures=[{"name": e.doc_id or "?", "error": str(e.payload.get("failure_reason") or e.payload.get("outcome")),
                   "at": _ts(e)} for e in reversed(fails)],
        openrouter=or_info if or_info and "error" not in or_info else None,
        today=today, host=socket.gethostname(), repo="mailroom-reloaded")


# ── settings, state, log ─────────────────────────────────────────────

LOG_MAX_BYTES = 5 * 1024 * 1024
BAD_NUMBER = "not a valid number — use E.164, e.g. +16085551234"
NO_NUMBER = "no number saved — use: pager on --to +1…"


def _file(name: str) -> Path:
    """Return a pager file path in the configured data directory, creating that directory."""
    return get_base_dir() / name


def read_settings() -> dict:
    """Return saved settings, or an empty dict for unreadable, invalid, or non-object JSON."""
    try:
        d = json.loads(_file("pager_settings.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(*, enabled: bool | None = None, recipients: list[str] | None = None, by: str) -> dict:
    """Persist supplied settings and return the updated mapping.

    None leaves the corresponding setting unchanged. Deduplicate recipients
    in input order and record ``by`` as the update source with a UTC timestamp.
    Filesystem and serialization errors propagate.
    """
    s = read_settings()
    if enabled is not None:
        s["enabled"] = bool(enabled)
    if recipients is not None:
        s["recipients"] = list(dict.fromkeys(recipients))
    s.update(updated_at=datetime.now(UTC).isoformat(timespec="seconds"), updated_by=by)
    _atomic_write(_file("pager_settings.json"), s)
    return s


def load_pager_config(env=None) -> PagerConfig:
    """Resolve defaults, taxonomy, environment, and saved settings in that precedence order.

    ``env`` defaults to the process environment. An unreadable taxonomy uses
    defaults; saved settings override the enabled flag and recipients.
    """
    from mailroom_reloaded.settings import load_taxonomy

    env = os.environ if env is None else env
    block = _source("taxonomy", lambda: load_taxonomy().raw.get("pager"), None)
    environment = (env.get("MAILROOM_ENVIRONMENT") or "live").strip() or "live"
    cfg = PagerConfig.resolve(block, env, environment=environment, settings=read_settings())
    for w in cfg.warnings:
        logger.warning("pager_config", detail=w)
    return cfg


def load_state():
    """Restore pager state, or return fresh state if reading or deserialization raises an Exception."""
    from mailroom_reloaded.obs.pager_core import PagerState

    try:
        return PagerState.from_json(json.loads(_file("pager_state.json").read_text()))
    except Exception:  # noqa: BLE001 — a corrupt state file restarts clean
        return PagerState()


def save_state(state) -> None:
    """Replace the saved pager state; serialization and filesystem errors propagate."""
    _atomic_write(_file("pager_state.json"), state.to_json())


def _mask(to: str) -> str:
    """Return the recipient masked for display."""
    from mailroom_reloaded.obs.sms import mask_number

    return mask_number(to)


def _log(payload: dict) -> None:
    """Append a JSON payload to the pager log, rotating an existing log over 5 MiB.

    Keep one backup at ``pager_log.jsonl.1``. Filesystem and serialization
    errors propagate.
    """
    path = _file("pager_log.jsonl")
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    with path.open("a") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ── the sender ───────────────────────────────────────────────────────


class Pager:
    """Scans every `scan.seconds`, digests on the clock, texts every recipient.

    One sender per data dir (flock on `pager.lock`); a second Pager stays
    passive. State lives in `pager_state.json`, so restarts and the CLI
    (`mute`) share it. `tick` never raises into its host."""

    def __init__(self, cfg: PagerConfig | None = None, *, transport=None):
        """Restore state and use the supplied config and transport, or resolve them as needed.

        Settings file changes are applied on later ticks even with an explicit
        config. Filesystem errors outside state loading can propagate.
        """
        self._own_cfg = cfg is None
        self.cfg = cfg or load_pager_config()
        self.transport, self.http, self._lock, self._last_scan = transport, None, None, None
        self._settings_sig = self._sig()
        self.state = load_state()  # in memory from here on: the disk copy only feeds `mute` back in
        self._mutex, self._stop = threading.Lock(), threading.Event()

    @staticmethod
    def _sig():
        """Return the settings file modification time and size, or None on a stat error."""
        try:
            st = _file("pager_settings.json").stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def _can_deliver(self) -> bool:
        """Return whether this pager's transport has what it needs to send.

        An injected transport or sms override counts; otherwise the configured
        transport must have all its credentials (console needs none).
        """
        from mailroom_reloaded.obs import sms

        return bool(self.transport or sms._OVERRIDE is not None or not _missing_env(self.cfg))

    def acquire(self) -> bool:
        """Keep an exclusive sender lock, returning False if acquisition fails.

        Return True when already held. Errors opening the lock file propagate.
        """
        import fcntl

        if self._lock:
            return True
        fh = open(_file("pager.lock"), "w")  # noqa: SIM115 — held for the process lifetime
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._lock = fh
        return True

    def release(self) -> None:
        """Close the held sender lock, allowing another pager to acquire it."""
        if self._lock:
            self._lock.close()
            self._lock = None

    def tick(self, now: float | None = None, *, force_scan: bool = False) -> list:
        """Process due scans and digests and return per-recipient send results.

        ``now`` is epoch seconds, defaulting to the current time. ``force_scan``
        bypasses the scan interval, but sending still follows pager gates.
        Inactive or passive pagers return an empty list. Exceptions are caught
        and return an empty list even if some side effects already occurred.
        """
        try:
            with self._mutex:
                return self._tick(time.time() if now is None else now, force_scan=force_scan)
        except Exception as exc:  # noqa: BLE001 — never raise into the watchdog/watcher/API
            logger.warning("pager_tick_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return []

    def flush(self, now: float | None = None) -> list:
        """Force a scan through ``tick`` and return its send results.

        Used at eval exit to attempt pending run events; quiet hours, mute, and
        rate limits can still delay them.
        """
        return self.tick(now, force_scan=True)

    def stop(self) -> None:
        """Signal the background loop to exit without joining it or releasing the sender lock."""
        self._stop.set()

    def _tick(self, now: float, force_scan: bool = False, override: dict | None = None) -> list:
        """Refresh saved settings and mute state, process due messages, and persist state.

        ``now`` is epoch seconds; ``override`` replaces collected snapshot fields.
        Return delivery results, or an empty list when inactive or unable to
        acquire the sender lock. State-save errors are suppressed; other errors
        propagate to ``tick``.
        """
        from dataclasses import replace

        from mailroom_reloaded.obs.pager_core import plan_digest, plan_scan

        sig = self._sig()
        if sig != self._settings_sig:  # `pager on/off/number` reach every host, explicit cfg or not
            self._settings_sig = sig
            self.cfg = load_pager_config() if self._own_cfg else self.cfg.apply_settings(read_settings())
        if self.cfg.active and not self._can_deliver():
            self.release()  # a sender that can't deliver must not hold the lock from one that can
            return []
        if not self.cfg.active or not self.acquire():
            return []
        self.state.muted_until = load_state().muted_until  # only `mute` writes state from outside
        cfg, msgs, snap = self.cfg, [], None
        window = cfg.digest_minutes(cfg.run_mode)

        def snapshot():
            """Collect the current window snapshot and apply any supplied field overrides."""
            s = collect_snapshot(cfg, window_minutes=window or 10, now=now)
            return replace(s, **override) if override else s
        if force_scan or self._last_scan is None or now - self._last_scan >= cfg.scan.seconds:
            snap, self._last_scan = snapshot(), now
            out, self.state = plan_scan(snap, cfg, self.state, now)
            msgs += out.messages
        out, self.state = plan_digest(lambda: snap or snapshot(), cfg, self.state, now)  # snapshots only if due
        msgs += out.messages
        results = [r for m in msgs for r in self._deliver(m, now)]
        _source("pager_state", lambda: save_state(self.state), None)
        return results

    def _deliver(self, msg, now: float) -> list:
        """Send a message to every recipient and return their send results.

        Mark it sent only once every recipient has succeeded (a retry texts just the rest). Add masked delivery records to the
        optional log and webhook. An unavailable transport yields no results;
        transport exceptions propagate, while log-write errors are suppressed.
        """
        from mailroom_reloaded.obs.pager_core import _recent, mark_sent
        from mailroom_reloaded.obs.sms import build_transport

        transport = self.transport or build_transport(self.cfg.transport)
        track = bool(msg.alert_keys)  # digests carry no alert keys: a new digest is new text, so no carried-over state
        st = self.state
        # incident identity: an alert that cleared and re-fired has a new `since`, so it never inherits old delivery state
        def ident(k: str) -> str:  # resolved messages: plan_scan moved the alert from st.open to st.recovered
            e = st.open.get(k) or st.recovered.get(k)
            return f"{k}@{e.get('since')}" if e else k
        mid = f"{msg.kind}|{','.join(sorted(ident(k) for k in msg.alert_keys))}"
        h = lambda to: hashlib.sha1(to.encode()).hexdigest()[:10]

        def live(k: str, v: dict) -> bool:  # kept while the incident is unacked; a 24h safety TTL otherwise
            if now - v.get("at", 0) >= 86400:
                return False
            kind, _, ids = k.partition("|")
            if kind not in ("alert", "resolved"):
                return True
            return all(ident(x.partition("@")[0]) == x for x in ids.split(","))
        st.delivered = {k: v for k, v in st.delivered.items() if live(k, v)}
        done = set(st.delivered.get(mid, {}).get("to", [])) if track else set()
        todo = [to for to in self.cfg.recipients if h(to) not in done]
        critical = msg.kind == "alert" and any(st.open.get(k, {}).get("severity") == "critical" for k in msg.alert_keys)
        if not critical:  # the hourly cap counts every send, so text only as many recipients as it still allows
            todo = todo[:max(0, self.cfg.alerts.max_texts_per_hour - _recent(st, now))]
        sent = [(to, transport.send(to, msg.text)) for to in todo] if transport else []
        done |= {h(to) for to, r in sent if r.ok}
        everyone = bool(self.cfg.recipients) and all(h(to) in done for to in self.cfg.recipients)
        acked = everyone if track else any(r.ok for _, r in sent)
        # ack only once every recipient has the text; each successful send still counts toward the hourly cap
        self.state = mark_sent(st, msg.kind, msg.alert_keys, now, acked, sends=sum(r.ok for _, r in sent))
        keep = {k: v for k, v in self.state.delivered.items() if k != mid}
        self.state.delivered = {**keep, mid: {"to": sorted(done), "at": now}} if track and done and not everyone else keep
        partial = track and bool(done) and not everyone
        payload = {**msg.payload, **({"delivery_partial": True} if partial else {}), "delivery":
                   [{"to": _mask(to), "ok": r.ok, "transport": r.transport,
                     "segments": r.segments, "detail": r.detail} for to, r in sent]}
        if self.cfg.machine.log:
            _source("pager_log", lambda: _log(payload), None)
        if self.cfg.machine.webhook_url:
            self._webhook(payload)
        if partial:
            logger.warning("pager_delivery_partial", kind=msg.kind, ref=msg.ref, ok=sum(r.ok for _, r in sent), to=len(sent))
        logger.info("pager_sent", kind=msg.kind, ref=msg.ref, ok=sum(r.ok for _, r in sent), to=len(sent))
        return [r for _, r in sent]

    def _webhook(self, payload: dict) -> None:
        """Post a payload once with a five-second HTTP timeout and optional HMAC signature.

        Request exceptions are suppressed and HTTP error statuses are ignored;
        JSON serialization errors propagate.
        """
        import hashlib
        import hmac

        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get("MAILROOM_PAGER_WEBHOOK_SECRET", "")
        if secret:
            headers["X-Mailroom-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            if self.http is not None:
                self.http.post(self.cfg.machine.webhook_url, content=body, headers=headers, timeout=5.0)
            else:
                with httpx.Client(timeout=5.0) as c:
                    c.post(self.cfg.machine.webhook_url, content=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 — fail-soft, no retries
            logger.warning("pager_webhook_failed", error=type(exc).__name__)

    def start_background(self, *, idle_ok: bool = False) -> threading.Thread | None:
        """Return a daemon polling thread, or None if inactive or another sender holds the lock.

        ``idle_ok`` keeps an inactive pager, or one blocked by another sender's
        lock, polling so a later ``pager on`` or the lock's release takes effect. Research mode registers a forced scan at process exit. Errors
        opening the sender lock or starting the thread propagate.
        """
        if not self.cfg.active and not idle_ok:
            return None
        if self.cfg.active and self._can_deliver() and not self.acquire():
            logger.info("pager_passive", reason="another process holds pager.lock")
            if not idle_ok:
                return None  # idle_ok hosts keep polling: _tick retries the lock if its holder exits

        def loop():
            """Tick until stopped, waiting at most 15 seconds between attempts."""
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(min(self.cfg.scan.seconds, 15))
        t = threading.Thread(target=loop, name="mailroom-pager", daemon=True)
        t.start()
        if self.cfg.run_mode == "research":
            import atexit

            atexit.register(self.flush)
        logger.info("pager_started", recipients=[_mask(r) for r in self.cfg.recipients], transport=self.cfg.transport)
        return t


# ── test text + start prompt ─────────────────────────────────────────


def send_test(cfg: PagerConfig, recipients) -> list:
    """Send a test message regardless of the enabled flag and return recipient/result pairs.

    Return an empty list when the transport is unavailable. Attempt to log
    the results even when machine logging is disabled; log errors are
    suppressed, while transport exceptions propagate.
    """
    from mailroom_reloaded.obs.pager_core import _clock
    from mailroom_reloaded.obs.sms import build_transport

    body = f"✅ MAILROOM PAGER TEST · {socket.gethostname()} · {_clock(time.time(), cfg.timezone, zone=True)}"
    t = build_transport(cfg.transport)
    if t is None:
        return []
    results = [(to, t.send(to, body)) for to in recipients]
    _source("pager_log", lambda: _log({"schema": "mailroom.pager/v1", "ref": None, "kind": "test",
                                        "sent_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                        "status": "healthy", "text": body, "delivery": [
                                            {"to": _mask(to), "ok": r.ok, "transport": r.transport,
                                             "segments": r.segments, "detail": r.detail} for to, r in results]}), None)
    return results


def _tty() -> bool:
    """Return whether both standard input and output are terminals."""
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(input_fn, question: str, timeout_s: float) -> str | None:
    """Ask once and return trimmed input, or None on timeout or caught input errors.

    ``timeout_s`` limits waiting only when ``input_fn`` is builtins.input;
    an injected callable manages its own timeout. EOFError, interruption,
    and exhausted iterators return None; EOF from built-in input's readline
    returns an empty string.
    """
    import builtins
    import select
    import sys

    try:
        if input_fn is builtins.input:
            print(question, end="", flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
            if not ready:
                print()
                return None
            return sys.stdin.readline().strip()
        return str(input_fn(question)).strip()
    except (TimeoutError, EOFError, KeyboardInterrupt, StopIteration):
        return None


def _yes(answer: str | None, default: bool) -> bool:
    """Use the default for empty answers; otherwise accept any answer starting with y, ignoring case."""
    return default if not answer else answer.lower().startswith("y")


def prompt_on_start(cfg: PagerConfig, *, flag: bool | None, input_fn=input, isatty=None,
                    timeout_s: float = 30.0) -> PagerConfig:
    """Resolve a saved pager toggle or prompt interactively for a production start.

    An explicit ``flag`` persists the toggle and skips prompting; enabling
    without recipients raises SystemExit(2). Otherwise prompt only on a
    terminal in production unless ``MAILROOM_PAGER_PROMPT=0``. Keep the
    current toggle on unanswered prompts; newly enabling can save a
    recipient and send a test text. ``timeout_s`` is the per-question wait
    for built-in input. Settings-write and test-send errors can propagate.
    """
    import sys

    from mailroom_reloaded.obs.pager_core import normalize_recipient

    if flag is not None:
        if flag and not cfg.recipients:
            print(f"--pager: {NO_NUMBER}", file=sys.stderr)
            raise SystemExit(2)
        write_settings(enabled=flag, by="flag")
        return load_pager_config()
    if cfg.run_mode != "production" or os.environ.get("MAILROOM_PAGER_PROMPT", "1") == "0":
        return cfg
    if not (isatty if isatty is not None else _tty()):
        logger.info("pager_prompt_skipped", alerts="ON" if cfg.active else "OFF",
                    hint="enable with: mailroom pager on --to +1…")
        return cfg
    if cfg.enabled and cfg.recipients:
        who = ", ".join(_mask(r) for r in cfg.recipients)
        if not _yes(_ask(input_fn, f"📱 Text alerts are ON → {who}. Keep them on? [Y/n]: ", timeout_s), True):
            write_settings(enabled=False, by="prompt")
        return load_pager_config()
    if not _yes(_ask(input_fn, "📱 Text alerts are OFF for this live run. Enable them? [y/N]: ", timeout_s), False):
        return cfg
    recips = list(cfg.recipients)
    if not recips:
        n = normalize_recipient(_ask(input_fn, "   Number to text (mobile or Google Voice, e.g. +16085551234): ",
                                     timeout_s) or "", cfg.transport)
        if not n:
            print(f"   {BAD_NUMBER} — text alerts stay OFF.")
            return cfg
        recips = [n]
    write_settings(enabled=True, recipients=recips, by="prompt")
    cfg = load_pager_config()
    if _yes(_ask(input_fn, "   Send a test text now? [Y/n]: ", timeout_s), True):
        for to, r in send_test(cfg, cfg.recipients):
            print(f"   {_mask(to)}: {'sent' if r.ok else 'FAILED — ' + r.detail}")
    return cfg


# ── CLI ──────────────────────────────────────────────────────────────


def _missing_env(cfg: PagerConfig) -> list[str]:
    """Return missing credential variable names for the configured transport."""
    if cfg.transport == "twilio":
        miss = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not os.environ.get(k)]
        if not (os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")):
            miss.append("TWILIO_FROM_NUMBER|TWILIO_MESSAGING_SERVICE_SID")
        return miss
    if cfg.transport == "email_gateway":
        return [f"MAILROOM_SMTP_{k}" for k in ("HOST", "USER", "PASSWORD") if not os.environ.get(f"MAILROOM_SMTP_{k}")]
    return []


def _read_log() -> list[dict]:
    """Read the current pager log, skipping invalid JSON lines; file read errors propagate."""
    path = _file("pager_log.jsonl")
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _summary_line(e: dict, tz: str) -> str:
    """Format a log entry time, kind, status, reference, and verdict using ? for invalid timestamps."""
    from mailroom_reloaded.obs.pager_core import _clock

    try:
        ts = datetime.fromisoformat(str(e.get("sent_at"))).timestamp()
        when = _clock(ts, tz)
    except ValueError:
        when = "?"
    lines = str(e.get("text", "")).splitlines()
    verdict = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    return f"{when} · {e.get('kind')} · {e.get('status')} · {e.get('ref')} · {verdict}"


def _duration(raw: str) -> float | None:
    """Parse a nonnegative integer with an h or m suffix into seconds, or return None."""
    m = re.fullmatch(r"(\d+)\s*([hm])", raw.strip().lower())
    return int(m[1]) * (3600 if m[2] == "h" else 60) if m else None


def main(argv: list[str] | None = None) -> int:
    """Run a pager subcommand using ``argv``, or process arguments when omitted.

    Commands inspect or persist settings, preview messages, send tests,
    change mute state, or run the sender indefinitely. Return 0 on success,
    1 on delivery failure or sender lock contention, and 2 for invalid input
    or an inactive run request. Argument parsing raises SystemExit for help
    or invalid syntax; unhandled filesystem and transport errors propagate.
    """
    import argparse
    import sys

    from mailroom_reloaded.obs.pager_core import (
        evaluate_rules,
        normalize_recipient,
        render_alert,
        render_digest,
    )

    ap = argparse.ArgumentParser(prog="mailroom pager", description="Operator SMS pager (text alerts).")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("status").add_argument("--full", action="store_true")
    sub.add_parser("on").add_argument("--to")
    sub.add_parser("off")
    num = sub.add_parser("number").add_subparsers(dest="op", required=True)
    for op in ("set", "add", "remove"):
        num.add_parser(op).add_argument("number")
    num.add_parser("list").add_argument("--show", action="store_true")
    sub.add_parser("test").add_argument("--to")
    pv = sub.add_parser("preview")
    pv.add_argument("--mode", choices=("production", "research"))
    pv.add_argument("--alert", action="store_true")
    pv.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=10)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--ref")
    sub.add_parser("mute").add_argument("duration", help="2h | 30m | off")
    sub.add_parser("run")
    a = ap.parse_args(argv)
    cfg = load_pager_config()

    def number(raw):
        """Normalize a recipient for the configured transport, reporting invalid input and returning None."""
        n = normalize_recipient(raw, cfg.transport)
        if not n:
            print(BAD_NUMBER, file=sys.stderr)
        return n

    if a.verb == "status":
        miss = _missing_env(cfg)
        st, last = load_state(), (_read_log() or [None])[-1]
        print(f"Text alerts: {'ON' if cfg.active else 'OFF'}" + ("" if cfg.recipients or not cfg.enabled else f" ({NO_NUMBER})"))
        print(f"Recipients: {', '.join(_mask(r) for r in cfg.recipients) or 'none'}")
        print(f"Transport: {cfg.transport} — " + (f"not deliverable, missing {', '.join(miss)}" if miss else "deliverable"))
        muted = st.muted_until and st.muted_until > time.time()
        print(f"Mode: {cfg.run_mode} · digest every {cfg.digest_minutes(cfg.run_mode):g} min · scan every {cfg.scan.seconds:g}s"
              + (" · MUTED" if muted else "") + (f" · quiet {cfg.quiet_hours.window}" if cfg.quiet else ""))
        print("Last text: " + (_summary_line(last, cfg.timezone) if last else "none"))
        if a.full:
            for k, src in sorted(cfg.sources.items()):
                print(f"  {k} = {_getdotted(cfg, k)!r}  ({src})")
        return 0
    if a.verb == "on":
        recips = list(cfg.recipients)
        if a.to:
            n = number(a.to)
            if not n:
                return 2
            recips = [n]
        if not recips and _tty():
            n = number(input("Number to text (mobile or Google Voice, e.g. +16085551234): "))
            recips = [n] if n else []
        if not recips:
            print(NO_NUMBER, file=sys.stderr)
            return 2
        write_settings(enabled=True, recipients=recips, by="cli")
        print(f"Text alerts ON → {', '.join(_mask(r) for r in recips)}")
        if _missing_env(cfg):
            print(f"warning: not deliverable yet — set {', '.join(_missing_env(cfg))}", file=sys.stderr)
        return 0
    if a.verb == "off":
        write_settings(enabled=False, by="cli")
        print("Text alerts OFF (numbers kept).")
        return 0
    if a.verb == "number":
        saved = list(read_settings().get("recipients") or [])
        if a.op == "list":
            print(f"{len(saved)} saved number(s):")
            for r in saved:
                print(f"  {r if a.show else _mask(r)}")
            return 0
        n = number(a.number)
        if not n:
            return 2
        saved = [n] if a.op == "set" else (saved + [n] if a.op == "add" else [r for r in saved if r != n])
        write_settings(recipients=saved, by="cli")
        print(f"{'removed' if a.op == 'remove' else 'saved'} {n}")
        return 0
    if a.verb == "test":
        to = [number(a.to)] if a.to else list(cfg.recipients)
        if not to or None in to:
            print(NO_NUMBER if not a.to else BAD_NUMBER, file=sys.stderr)
            return 2
        results = send_test(cfg, to)
        if not results:
            print(f"not deliverable — missing {', '.join(_missing_env(cfg)) or 'transport'}", file=sys.stderr)
            return 1
        for t, r in results:
            print(f"{_mask(t)}: ok={r.ok} transport={r.transport} segments={r.segments} {r.detail}")
        return 0 if all(r.ok for _, r in results) else 1
    if a.verb == "preview":
        from dataclasses import replace

        mode = a.mode or cfg.run_mode
        snap = collect_snapshot(replace(cfg, run_mode=mode), window_minutes=cfg.digest_minutes(mode) or 10)

        snap, state = replace(snap, mode=mode), load_state()
        if a.alert:
            alerts = sorted(evaluate_rules(snap, cfg, {}), key=lambda x: x.severity != "critical")
            if not alerts:
                print("No alerts are firing — nothing would be texted.")
                return 0
            msg = render_alert(alerts, snap, cfg, state, snap.taken_at)
        else:
            from mailroom_reloaded.obs.pager_core import plan_scan

            _, state = plan_scan(snap, cfg, state, snap.taken_at)  # fold in this snapshot's firing rules (nothing is sent or saved)
            msg = render_digest(snap, cfg, state, snap.taken_at)
        print(json.dumps(msg.payload, indent=1, ensure_ascii=False) if a.json else msg.text)
        return 0
    if a.verb == "log":
        entries = [e for e in _read_log() if not a.ref or e.get("ref") == a.ref][-a.n:]
        for e in entries:
            print(json.dumps(e, ensure_ascii=False) if a.json else _summary_line(e, cfg.timezone))
        return 0
    if a.verb == "mute":
        st = load_state()
        secs = None if a.duration == "off" else _duration(a.duration)
        if a.duration != "off" and secs is None:
            print("use: pager mute 2h | 30m | off", file=sys.stderr)
            return 2
        st.muted_until = time.time() + secs if secs else None
        save_state(st)
        print("Alerts unmuted." if secs is None else f"Muted (criticals still text) for {a.duration}.")
        return 0
    if a.verb == "run":
        p = Pager()
        if not p.cfg.active:
            print("Text alerts are OFF — enable with: mailroom pager on --to +1…", file=sys.stderr)
            return 2
        if not p.acquire():
            print("another process is already the pager sender for this data dir", file=sys.stderr)
            return 1
        while True:
            p.tick()
            time.sleep(min(p.cfg.scan.seconds, 15))
    return 2


def _getdotted(cfg: PagerConfig, key: str):
    """Look up a dotted config path through attributes and dict keys; missing attributes raise AttributeError."""
    obj = cfg
    for part in key.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part)
    return obj
