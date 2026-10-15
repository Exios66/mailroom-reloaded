"""Operator SMS pager host: ledger-backed snapshot, scheduler, settings, CLI.

The pure decisions (rules, frequency state machine, SMS copy, JSON payload)
live in ``obs/pager_core.py`` (verbatim from llm-mailroom); this module
collects a ``RunSnapshot`` from the archive ledger, the bins, the watcher lock,
passive provider health, Modal and OpenRouter, and sends through ``obs/sms``.
CLI: ``mailroom pager <verb>`` (see ``main``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

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
    base = Path(get_settings().base_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# ── providers ────────────────────────────────────────────────────────

_OR_CACHE: dict = {}


def openrouter_info(cfg: PagerConfig, client: httpx.Client | None = None) -> dict | None:
    """OpenRouter key status (``GET /key``), cached ``providers.openrouter_cache_minutes``."""
    key = (get_settings().openrouter_api_key or "").strip()
    if not key or key == "mock-key":
        return None
    if _OR_CACHE and time.time() - _OR_CACHE["at"] < cfg.providers.openrouter_cache_minutes * 60:
        return _OR_CACHE["info"]
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
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
    return ["vllm"] if get_settings().provider == "vllm" else []


def provider_statuses(cfg: PagerConfig, health: dict, or_info: dict | None, warm: dict | None) -> list[ProviderStatus]:
    """Pure: map passive health + Modal warm counts + OpenRouter /key onto provider states."""
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
        elif h.get("count_503"):
            st, det = "degraded", f"cold-start 503s {h['count_503']}"
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
    return f"{n} container{'' if n == 1 else 's'}"


# ── snapshot (archive ledger) ─────────────────────────────────────────


def _source(name: str, fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one dead source never blanks the digest
        logger.warning("pager_snapshot_source_failed", source=name, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return default


def _ts(entry) -> float:
    return datetime.fromisoformat(str(entry.ts)).timestamp()


def _closed_since(ledger, since: float, run_id: str | None) -> list:
    """``doc_closed`` entries at or after ``since`` (newest first, paged)."""
    out, offset = [], 0
    while True:
        page = ledger.entries(kind="doc_closed", run_id=run_id, limit=500, offset=offset, descending=True)
        for e in page:
            if _ts(e) < since:
                return out
            out.append(e)
        if len(page) < 500:
            return out
        offset += 500


def _doc_cost(e) -> float:
    return sum(float((u or {}).get("cost_usd") or 0) for u in (e.payload.get("usage_by_role") or {}).values())


def window_stats(entries: list) -> dict:
    """Pure: terminal outcomes, spend and median latency of ``doc_closed`` entries."""
    outs = [e.payload.get("outcome") for e in entries]
    lats = [float(e.payload["duration_s"]) for e in entries if e.payload.get("duration_s")]
    return {"received": len(entries), "archived": outs.count("completed"), "review": outs.count("parked"),
            "failed": outs.count("failed") + outs.count("aborted"),
            "cost_usd": round(sum(_doc_cost(e) for e in entries), 4),
            "p50_latency_s": statistics.median(lats) if lats else None, "first_pass_rate": None}


def _eval_score(run_id: str) -> float | None:
    from sqlalchemy import text

    from mailroom_reloaded.eval.runner import _engine

    with _engine().connect() as conn:
        v = conn.execute(text("SELECT AVG(overall_score) FROM eval_docs WHERE run_id = :r AND overall_score IS NOT NULL"),
                         {"r": run_id}).scalar()
    return None if v is None else float(v)


def eval_runs(ledger, now: float) -> list[RunProgress]:
    """Open ``eval`` runs as research progress: done = documents closed, total from ``run_opened``."""
    out = []
    for run_id in ledger.open_runs(kind="eval"):
        opened = (ledger.entries(run_id=run_id, kind="run_opened", limit=1) or [None])[0]
        if opened is None:
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
                               _ts(closed[0]) if closed else _ts(opened), False, metrics))
    return out


def watcher_running(base: Path) -> bool | None:
    """True when another process holds ``watcher.lock`` (a free lock means no watcher)."""
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
    from zoneinfo import ZoneInfo

    d = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now).astimezone()
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                           cwd=Path(__file__).resolve().parent, check=False)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _count(d: Path) -> int:
    return sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".meta"))


def collect_snapshot(cfg: PagerConfig, *, window_minutes: float, now: float | None = None) -> RunSnapshot:
    """Gather everything the pager reports. Each source fails soft on its own."""
    from mailroom_reloaded.llm.health import read_provider_health
    from mailroom_reloaded.obs.run_context import live_run_id
    from mailroom_reloaded.storage.bins import Bins
    from mailroom_reloaded.storage.ledger import get_ledger

    now = time.time() if now is None else now
    base, bins = get_base_dir(), Bins(get_base_dir())
    ledger = _source("ledger", get_ledger, None)
    run_id = live_run_id(datetime.fromtimestamp(now, UTC)) if cfg.run_mode == "production" else None
    queues = {"inbox": _source("inbox", lambda: _count(bins.inbox), 0),
              "processing": _source("processing", lambda: sum(
                  1 for p in (base / "processing").rglob("*") if p.is_file()) if (base / "processing").exists() else 0, 0),
              "review": _source("review", lambda: _count(bins.review), 0),
              "failed": _source("failed", lambda: _count(bins.failed), 0)}
    recent = _source("ledger_window", lambda: _closed_since(ledger, now - window_minutes * 60, run_id), [])
    window = {"minutes": window_minutes, **window_stats(recent)} if ledger is not None else {}
    today_entries = _source("ledger_today", lambda: _closed_since(
        ledger, _local_midnight(now, cfg.timezone).timestamp(), run_id), None)
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
                   "at": e.ts} for e in reversed(fails)],
        openrouter=or_info if or_info and "error" not in or_info else None,
        today=today, host=socket.gethostname(), repo="mailroom-reloaded")


# ── settings, state, log ─────────────────────────────────────────────

LOG_MAX_BYTES = 5 * 1024 * 1024
BAD_NUMBER = "not a valid number — use E.164, e.g. +16085551234"
NO_NUMBER = "no number saved — use: pager on --to +1…"


def _file(name: str) -> Path:
    return get_base_dir() / name


def read_settings() -> dict:
    try:
        d = json.loads(_file("pager_settings.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(*, enabled: bool | None = None, recipients: list[str] | None = None, by: str) -> dict:
    s = read_settings()
    if enabled is not None:
        s["enabled"] = bool(enabled)
    if recipients is not None:
        s["recipients"] = list(dict.fromkeys(recipients))
    s.update(updated_at=datetime.now(UTC).isoformat(timespec="seconds"), updated_by=by)
    _atomic_write(_file("pager_settings.json"), s)
    return s


def load_pager_config(env=None) -> PagerConfig:
    from mailroom_reloaded.settings import load_taxonomy

    env = os.environ if env is None else env
    block = _source("taxonomy", lambda: load_taxonomy().raw.get("pager"), None)
    environment = (env.get("MAILROOM_ENVIRONMENT") or "live").strip() or "live"
    cfg = PagerConfig.resolve(block, env, environment=environment, settings=read_settings())
    for w in cfg.warnings:
        logger.warning("pager_config", detail=w)
    return cfg


def load_state():
    from mailroom_reloaded.obs.pager_core import PagerState

    try:
        return PagerState.from_json(json.loads(_file("pager_state.json").read_text()))
    except Exception:  # noqa: BLE001 — a corrupt state file restarts clean
        return PagerState()


def save_state(state) -> None:
    _atomic_write(_file("pager_state.json"), state.to_json())


def _mask(to: str) -> str:
    from mailroom_reloaded.obs.sms import mask_number

    return mask_number(to)


def _log(payload: dict) -> None:
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
        self._own_cfg = cfg is None
        self.cfg = cfg or load_pager_config()
        self.transport, self.http, self._lock, self._last_scan = transport, None, None, None
        self._settings_sig = self._sig()
        self.state = load_state()

    @staticmethod
    def _sig():
        try:
            st = _file("pager_settings.json").stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def acquire(self) -> bool:
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
        if self._lock:
            self._lock.close()
            self._lock = None

    def tick(self, now: float | None = None) -> list:
        try:
            return self._tick(time.time() if now is None else now)
        except Exception as exc:  # noqa: BLE001 — never raise into the watchdog/watcher/API
            logger.warning("pager_tick_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return []

    def _tick(self, now: float, force_scan: bool = False, override: dict | None = None) -> list:
        from dataclasses import replace

        from mailroom_reloaded.obs.pager_core import plan_digest, plan_scan

        sig = self._sig()
        if self._own_cfg and sig != self._settings_sig:
            self.cfg, self._settings_sig = load_pager_config(), sig
        if not self.cfg.active or not self.acquire():
            return []
        cfg, self.state, msgs, snap = self.cfg, load_state(), [], None
        window = cfg.digest_minutes(cfg.run_mode)

        def snapshot():
            s = collect_snapshot(cfg, window_minutes=window or 10, now=now)
            return replace(s, **override) if override else s
        if force_scan or self._last_scan is None or now - self._last_scan >= cfg.scan.seconds:
            snap, self._last_scan = snapshot(), now
            out, self.state = plan_scan(snap, cfg, self.state, now)
            msgs += out.messages
        st = self.state
        if st.next_digest is None or now >= st.next_digest or st.in_quiet:
            out, self.state = plan_digest(snap or snapshot(), cfg, self.state, now)
            msgs += out.messages
        results = [r for m in msgs for r in self._deliver(m, now)]
        save_state(self.state)
        return results

    def _deliver(self, msg, now: float) -> list:
        from mailroom_reloaded.obs.pager_core import mark_sent
        from mailroom_reloaded.obs.sms import build_transport

        transport = self.transport or build_transport(self.cfg.transport)
        sent = [(to, transport.send(to, msg.text)) for to in self.cfg.recipients] if transport else []
        self.state = mark_sent(self.state, msg.kind, msg.alert_keys, now, any(r.ok for _, r in sent))
        payload = {**msg.payload, "delivery": [{"to": _mask(to), "ok": r.ok, "transport": r.transport,
                                                "segments": r.segments, "detail": r.detail} for to, r in sent]}
        if self.cfg.machine.log:
            _source("pager_log", lambda: _log(payload), None)
        if self.cfg.machine.webhook_url:
            self._webhook(payload)
        logger.info("pager_sent", kind=msg.kind, ref=msg.ref, ok=sum(r.ok for _, r in sent), to=len(sent))
        return [r for _, r in sent]

    def _webhook(self, payload: dict) -> None:
        import hashlib
        import hmac

        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get("MAILROOM_PAGER_WEBHOOK_SECRET", "")
        if secret:
            headers["X-Mailroom-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            (self.http or httpx.Client(timeout=5.0)).post(self.cfg.machine.webhook_url, content=body,
                                                          headers=headers, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 — fail-soft, no retries
            logger.warning("pager_webhook_failed", error=type(exc).__name__)

    def start_background(self) -> threading.Thread | None:
        if not self.cfg.active:
            return None
        if not self.acquire():
            logger.info("pager_passive", reason="another process holds pager.lock")
            return None

        def loop():
            while True:
                self.tick()
                time.sleep(min(self.cfg.scan.seconds, 15))
        t = threading.Thread(target=loop, name="mailroom-pager", daemon=True)
        t.start()
        logger.info("pager_started", recipients=[_mask(r) for r in self.cfg.recipients], transport=self.cfg.transport)
        return t


# ── test text + start prompt ─────────────────────────────────────────


def send_test(cfg: PagerConfig, recipients) -> list:
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
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(input_fn, question: str, timeout_s: float) -> str | None:
    """One question; None on timeout/EOF so the caller takes the default."""
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
    return default if not answer else answer.lower().startswith("y")


def prompt_on_start(cfg: PagerConfig, *, flag: bool | None, input_fn=input, isatty=None,
                    timeout_s: float = 30.0) -> PagerConfig:
    """Live-production start: ask whether to text alerts (default: leave as is)."""
    import sys

    from mailroom_reloaded.obs.pager_core import normalize_number

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
        n = normalize_number(_ask(input_fn, "   Number to text (mobile or Google Voice, e.g. +16085551234): ",
                                  timeout_s) or "")
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
    if cfg.transport == "twilio":
        miss = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not os.environ.get(k)]
        if not (os.environ.get("TWILIO_FROM_NUMBER") or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")):
            miss.append("TWILIO_FROM_NUMBER|TWILIO_MESSAGING_SERVICE_SID")
        return miss
    if cfg.transport == "email_gateway":
        return [f"MAILROOM_SMTP_{k}" for k in ("HOST", "USER", "PASSWORD") if not os.environ.get(f"MAILROOM_SMTP_{k}")]
    return []


def _read_log() -> list[dict]:
    path = _file("pager_log.jsonl")
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _summary_line(e: dict, tz: str) -> str:
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
    m = re.fullmatch(r"(\d+)\s*([hm])", raw.strip().lower())
    return int(m[1]) * (3600 if m[2] == "h" else 60) if m else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from mailroom_reloaded.obs.pager_core import (
        evaluate_rules,
        normalize_number,
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
        n = normalize_number(raw)
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
        mode = a.mode or cfg.run_mode
        snap = collect_snapshot(cfg, window_minutes=cfg.digest_minutes(mode) or 10)
        from dataclasses import replace

        snap, state = replace(snap, mode=mode), load_state()
        if a.alert:
            alerts = sorted(evaluate_rules(snap, cfg, {}), key=lambda x: x.severity != "critical")
            if not alerts:
                print("No alerts are firing — nothing would be texted.")
                return 0
            msg = render_alert(alerts, snap, cfg, state, snap.taken_at)
        else:
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
    obj = cfg
    for part in key.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part)
    return obj
