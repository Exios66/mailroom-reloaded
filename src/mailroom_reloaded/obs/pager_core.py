"""Operator SMS pager core (shared verbatim with llm-mailroom/src/pipeline/pager_core.py).

Pure and stdlib-only: config resolution, alert rules, the frequency state
machine and the SMS/JSON renderers. No I/O, no clock reads — `now` is always
a parameter. The host module (pager.py) collects snapshots and sends.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

SCHEMA = "mailroom.pager/v1"
RESEARCH_ENVS = {"pilot", "misc", "mock", "eval"}
ENUMS = {"transport": ("twilio", "email_gateway", "console"), "mode": ("auto", "production", "research"),
         "style": ("emoji", "ascii"), "quiet_hours.allow": ("none", "critical", "all")}
FLOORS = {"scan.seconds": 15, "max_chars": 160}
ENV = {"MAILROOM_PAGER": ("enabled",), "MAILROOM_PAGER_TO": ("recipients",),
       "MAILROOM_PAGER_TRANSPORT": ("transport",), "MAILROOM_PAGER_MODE": ("mode",),
       "MAILROOM_PAGER_STYLE": ("style",), "MAILROOM_PAGER_TZ": ("timezone",),
       "MAILROOM_PAGER_DIGEST_MINUTES": ("digest.production_minutes", "digest.research_minutes"),
       "MAILROOM_PAGER_SCAN_SECONDS": ("scan.seconds",), "MAILROOM_PAGER_QUIET_HOURS": ("quiet_hours.window",),
       "MAILROOM_PAGER_WEBHOOK_URL": ("machine.webhook_url",)}
ASCII = {"🔴 Action needed:": "ACTION:", "🟠 Watch:": "WATCH:", "🟢": "OK:", "🟠": "WATCH:", "🔴": "ACTION:", "⏸": "PAUSED:", "✅": "OK:", "▶": "START:",
         "·": "-", "—": "-", "`": "'", "×": "x", "•": "*"}


@dataclass(frozen=True)
class Digest:
    production_minutes: float = 10
    research_minutes: float = 10
    align_to_clock: bool = True
    when_idle: bool = True
    idle_heartbeat_minutes: float = 60


@dataclass(frozen=True)
class Scan:
    seconds: float = 60
    degraded_after_scans: int = 2
    recover_after_scans: int = 2


@dataclass(frozen=True)
class Alerts:
    critical_cooldown_minutes: float = 15
    warning_cooldown_minutes: float = 60
    send_recovered: bool = True
    max_texts_per_hour: int = 12


@dataclass(frozen=True)
class QuietHours:
    window: str = ""
    allow: str = "critical"
    catch_up_digest: bool = True


@dataclass(frozen=True)
class Providers:
    openrouter_cache_minutes: float = 5
    probe_modal: bool = False


@dataclass(frozen=True)
class Machine:
    ref_in_text: bool = True
    log: bool = True
    webhook_url: str = ""


SECTIONS = {"digest": Digest, "scan": Scan, "alerts": Alerts, "quiet_hours": QuietHours,
            "providers": Providers, "machine": Machine}
RULES = {"watcher_down": {}, "provider_down": {}, "provider_degraded": {"error_rate": 0.25, "min_calls": 4},
         "openrouter_credit": {"below_usd": 1.0}, "docs_failed": {"critical_at": 5},
         "review_backlog": {"threshold": 20}, "stuck_docs": {"minutes": 15}, "ingestion_paused": {},
         "outbox_dead": {}, "run_stalled": {"minutes": 15}}
DEFAULTS = {"enabled": False, "recipients": [], "transport": "twilio", "mode": "auto", "style": "emoji",
            "max_chars": 640, "timezone": "", **{k: asdict(c()) for k, c in SECTIONS.items()},
            "rules": {k: {"enabled": True, **v} for k, v in RULES.items()}}


def _flat(d: Mapping, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping) and (not prefix or prefix == "rules."):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def _coerce(default, v):
    if isinstance(default, bool):
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(v)
    if isinstance(default, (int, float)):
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(v)  # noqa: TRY004 — callers catch ValueError as "invalid value"
        return float(v) if isinstance(v, str) and "." in v else (int(v) if isinstance(v, str) else v)
    if isinstance(default, list):
        items = v.split(",") if isinstance(v, str) else v
        if not isinstance(items, (list, tuple)):
            raise ValueError(v)  # noqa: TRY004
        return [str(x).strip() for x in items if str(x).strip()]
    if not isinstance(v, str):
        raise ValueError(v)  # noqa: TRY004
    return v.strip()


def normalize_number(raw: str) -> str | None:
    s = re.sub(r"[\s\-.()]", "", str(raw or ""))
    if re.fullmatch(r"\d{10}", s):
        s = "+1" + s
    elif re.fullmatch(r"1\d{10}", s):
        s = "+" + s
    return s if re.fullmatch(r"\+[1-9]\d{7,14}", s) else None


def normalize_recipient(raw: str, transport: str) -> str | None:
    """E.164 number, or a carrier email-to-SMS address when the transport is `email_gateway`."""
    if transport == "email_gateway":
        s = str(raw or "").strip().lower()
        return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", s) else None
    return normalize_number(raw)


def _valid_tz(tz: str) -> bool:
    try:
        ZoneInfo(tz) if tz else None
        return True
    except (ValueError, KeyError, OSError):  # ZoneInfoNotFoundError subclasses KeyError
        return False


def _parse_quiet(window: str):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", window or "")
    if not m:
        return None
    try:
        return time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4]))
    except ValueError:
        return None


@dataclass(frozen=True)
class PagerConfig:
    enabled: bool
    recipients: tuple[str, ...]
    transport: str
    mode: str
    style: str
    max_chars: int
    timezone: str
    digest: Digest
    scan: Scan
    alerts: Alerts
    quiet_hours: QuietHours
    providers: Providers
    machine: Machine
    rules: dict[str, dict]
    environment: str = ""
    run_mode: str = "production"
    quiet: tuple[time, time] | None = None
    sources: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @classmethod
    def resolve(cls, block: Mapping | None, env: Mapping[str, str], *, environment: str,
                settings: Mapping | None = None) -> PagerConfig:
        defaults, warn = _flat(DEFAULTS), []
        if block is not None and not isinstance(block, Mapping):
            warn.append(f"pager_config_invalid: pager block must be a mapping, got {type(block).__name__}")
            block = None
        if settings is not None and not isinstance(settings, Mapping):
            settings = None
        vals, sources = dict(defaults), {k: "default" for k in defaults}
        layers = [("taxonomy", _flat(block or {})),
                  ("env", {k: env[e] for e, ks in ENV.items() if env.get(e, "").strip() for k in ks}),
                  ("settings", {k: (settings or {})[k] for k in ("enabled", "recipients") if k in (settings or {})})]
        for src, layer in layers:
            for k, v in layer.items():
                if k not in defaults:
                    warn.append(f"pager_config_unknown_key: {k}")
                    continue
                try:
                    v = _coerce(defaults[k], v)
                    if k in ENUMS and v not in ENUMS[k]:
                        raise ValueError(v)
                    if k == "timezone" and not _valid_tz(v):
                        raise ValueError(v)
                except (ValueError, TypeError):
                    warn.append(f"pager_config_invalid: {k}={v!r} ({src})")
                    continue
                if k in FLOORS:
                    v = max(FLOORS[k], v)
                elif k.endswith(("minutes", "_scans")):
                    v = max(0, v)
                vals[k], sources[k] = v, src
        recips = _recipients(vals["recipients"], vals["transport"], warn)
        quiet = _parse_quiet(vals["quiet_hours.window"])
        if vals["quiet_hours.window"] and quiet is None:
            warn.append(f"pager_config_invalid: quiet_hours.window={vals['quiet_hours.window']!r}")
        sect = {n: c(**{f.name: vals[f"{n}.{f.name}"] for f in fields(c)}) for n, c in SECTIONS.items()}
        rules = {r: {k: vals[f"rules.{r}.{k}"] for k in ("enabled", *RULES[r])} for r in RULES}
        mode = vals["mode"] if vals["mode"] != "auto" else (
            "research" if environment.lower() in RESEARCH_ENVS else "production")
        return cls(enabled=vals["enabled"], recipients=tuple(dict.fromkeys(recips)), transport=vals["transport"],
                   mode=vals["mode"], style=vals["style"], max_chars=int(vals["max_chars"]),
                   timezone=vals["timezone"], rules=rules, environment=environment, run_mode=mode, quiet=quiet,
                   sources=sources, warnings=tuple(warn), **sect)

    def apply_settings(self, settings: Mapping | None) -> PagerConfig:
        """Re-apply the CLI's saved toggle/numbers (`pager_settings.json`) onto a host-built config."""
        from dataclasses import replace

        s, warn = settings if isinstance(settings, Mapping) else {}, list(self.warnings)
        out = {}
        if isinstance(s.get("enabled"), bool):
            out["enabled"] = s["enabled"]
        if isinstance(s.get("recipients"), list):
            out["recipients"] = tuple(dict.fromkeys(_recipients(s["recipients"], self.transport, warn)))
        return replace(self, warnings=tuple(warn), **out)

    @property
    def active(self) -> bool:
        """Sending needs the toggle ON and at least one valid recipient."""
        return self.enabled and bool(self.recipients)

    def digest_minutes(self, mode: str) -> float:
        return self.digest.production_minutes if mode == "production" else self.digest.research_minutes


def _recipients(raw: list, transport: str, warn: list) -> list[str]:
    out = []
    for r in raw:
        n = normalize_recipient(r, transport)
        if n:
            out.append(n)
        else:
            warn.append(f"pager_config_invalid: recipient {str(r)[:3]}…")
    return out


# ── data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    state: Literal["up", "warm", "idle", "degraded", "down", "unknown"]
    detail: str = ""
    calls: int = 0
    errors: int = 0


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    label: str
    total: int
    done: int
    started_at: float
    updated_at: float
    finished: bool
    metrics: dict


@dataclass(frozen=True)
class RunSnapshot:
    taken_at: float
    mode: str
    watcher_alive: bool | None
    heartbeat_age_s: float | None
    uptime_s: float | None
    sha: str | None
    queues: dict[str, int]
    window: dict
    stuck: int
    paused: dict | None
    outbox_dead: int | None
    providers: list[ProviderStatus]
    runs: list[RunProgress]
    failures: list[dict]
    openrouter: dict | None
    today: dict | None = None
    host: str = ""
    repo: str = ""


@dataclass(frozen=True)
class Alert:
    key: str
    severity: Literal["critical", "warning"]
    title: str
    detail: str = ""
    next_step: str = ""
    since: float | None = None


@dataclass
class PagerState:
    open: dict[str, dict] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    sent_log: list[float] = field(default_factory=list)
    next_digest: float | None = None
    last_idle_digest: float | None = None
    muted_until: float | None = None
    in_quiet: bool = False
    held: list[str] = field(default_factory=list)
    runs_seen: dict[str, str] = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    recovered: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Mapping) -> PagerState:
        names = {f.name for f in fields(cls)}
        return cls(**copy.deepcopy({k: v for k, v in (d or {}).items() if k in names}))


@dataclass(frozen=True)
class Message:
    kind: str
    ref: str
    text: str
    payload: dict
    alert_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outbox:
    messages: list[Message]


# ── rules ────────────────────────────────────────────────────────────


def _display(name: str) -> str:
    if name.startswith("modal:"):
        return f"Modal {name[6:]}"
    return {"openrouter": "OpenRouter"}.get(name, name)


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def evaluate_rules(snap: RunSnapshot, cfg: PagerConfig, baseline: dict) -> list[Alert]:
    R, out, tz, now = cfg.rules, [], cfg.timezone, snap.taken_at

    def add(key, sev, title, nxt, detail=""):
        out.append(Alert(key, sev, title, detail, nxt, baseline.get(key, now)))

    if R["watcher_down"]["enabled"] and snap.watcher_alive is False and snap.mode != "research":
        why = (f"no heartbeat for {_dur(snap.heartbeat_age_s)}" if snap.heartbeat_age_s is not None
               else "no watcher process is running")
        cmd = "mailroom watch" if snap.repo == "mailroom-reloaded" else "python -m pipeline.watcher"
        add("watcher_down", "critical", f"the watcher has stopped ({why})", f"restart with `{cmd}`")
    for p in snap.providers:
        detail = f"{p.errors} of {p.calls} calls failed ({p.detail})" if p.calls and p.detail else p.detail
        if p.state == "down" and R["provider_down"]["enabled"]:
            if p.name.startswith("modal:"):
                add(f"provider_down:{p.name}", "critical", f"{_display(p.name)} tier is down",
                    "check `modal app logs mailroom-vllm`", detail)
            else:
                add(f"provider_down:{p.name}", "critical", f"{_display(p.name)} is rejecting requests ({p.detail})",
                    "check OPENROUTER_API_KEY and openrouter.ai/settings/credits", detail)
        rd = R["provider_degraded"]
        rate_bad = p.calls >= rd["min_calls"] and p.errors / p.calls >= rd["error_rate"]
        if p.state != "down" and (p.state == "degraded" or rate_bad) and rd["enabled"]:
            why = p.detail or f"{p.errors} of {p.calls} calls failed"
            add(f"provider_degraded:{p.name}", "warning", f"{_display(p.name)} is degraded ({why})",
                "no action unless it persists; `pager status` for detail")
    left = (snap.openrouter or {}).get("limit_remaining")
    if R["openrouter_credit"]["enabled"] and left is not None and left < R["openrouter_credit"]["below_usd"]:
        add("openrouter_credit", "warning", f"OpenRouter credit is low (${left:.2f} left)",
            "top up at openrouter.ai/settings/credits")
    failed = int((snap.window or {}).get("failed") or 0)
    if R["docs_failed"]["enabled"] and failed > 0:
        ats = [f["at"] for f in snap.failures if f.get("at")]
        when = f"since {_clock(min(ats), tz)}" if ats else f"in the last {(snap.window or {}).get('minutes', 10)} min"
        add("docs_failed", "critical" if failed >= R["docs_failed"]["critical_at"] else "warning",
            f"{_n(failed, 'document')} failed {when}", "inspect data/failed/ and the latest error above")
    review = int((snap.queues or {}).get("review") or 0)
    if R["review_backlog"]["enabled"] and review > R["review_backlog"]["threshold"]:
        add("review_backlog", "warning", f"review queue at {review} (limit {R['review_backlog']['threshold']})",
            "resolve items in the review queue")
    if R["stuck_docs"]["enabled"] and snap.stuck:
        add("stuck_docs", "warning", f"{_n(snap.stuck, 'document')} stuck over {R['stuck_docs']['minutes']} min",
            "check data/processing/ and the watcher log")
    if R["ingestion_paused"]["enabled"] and snap.paused:
        add("ingestion_paused", "warning", f"ingestion paused by {snap.paused.get('actor', 'unknown')}: "
            f"{snap.paused.get('reason', 'no reason given')}", "clear the pause once resolved (see deploy/README.md)")
    if R["outbox_dead"]["enabled"] and snap.outbox_dead:
        add("outbox_dead", "warning", f"{snap.outbox_dead} echo emails could not be delivered",
            "check the Gmail channel credentials")
    for r in snap.runs:
        if R["run_stalled"]["enabled"] and not r.finished and now - r.updated_at >= R["run_stalled"]["minutes"] * 60:
            add(f"run_stalled:{r.run_id}", "warning",
                f"run {r.run_id} has made no progress for {round((now - r.updated_at) / 60)} min",
                "check the run's console/log")
    return out


def _resolved_phrase(key: str) -> str:
    kind, _, name = key.partition(":")
    if kind == "provider_down":
        return f"{_display(name)} tier is back up" if name.startswith("modal:") else f"{_display(name)} is accepting requests again"
    return {"provider_degraded": f"{_display(name)} is healthy again", "watcher_down": "The watcher is running again",
            "openrouter_credit": "OpenRouter credit is topped up", "docs_failed": "Document failures have stopped",
            "review_backlog": "The review queue is back under its limit", "stuck_docs": "No documents are stuck",
            "ingestion_paused": "Ingestion has resumed", "outbox_dead": "Echo email delivery has recovered",
            "run_stalled": f"Run {name} is progressing again"}.get(kind, f"{key} cleared")


# ── frequency state machine ──────────────────────────────────────────


def _local(now: float, tz: str) -> datetime:
    return datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now).astimezone()


def _clock(now: float, tz: str, zone: bool = False) -> str:
    d = _local(now, tz)
    s = f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    return f"{s} {d.tzname()}" if zone else s


def _dur(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{max(m, 1)} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def _in_quiet(cfg: PagerConfig, now: float) -> bool:
    if not cfg.quiet:
        return False
    t, (a, b) = _local(now, cfg.timezone).time(), cfg.quiet
    return a <= t < b if a < b else (t >= a or t < b)


def _gate(cfg: PagerConfig, state: PagerState, now: float, critical: bool) -> bool:
    """Gate 1: mute + quiet hours. True when a text of this severity may go out."""
    if state.muted_until and now < state.muted_until and not critical:
        return False
    if _in_quiet(cfg, now):
        return cfg.quiet_hours.allow == "all" or (critical and cfg.quiet_hours.allow == "critical")
    return True


def _recent(state: PagerState, now: float) -> int:
    return sum(1 for t in state.sent_log if now - t < 3600)


def _alert_of(key: str, e: dict) -> Alert:
    return Alert(key, e["severity"], e["title"], e.get("detail", ""), e.get("next_step", ""), e.get("since"))


def _active(state: PagerState, cfg: PagerConfig) -> list[Alert]:
    """Open alerts an operator should hear about: criticals, debounced warnings, anything already texted."""
    act = [_alert_of(k, e) for k, e in state.open.items() if e["fail"] > 0 and (
        e["severity"] == "critical" or e["fail"] >= cfg.scan.degraded_after_scans or e.get("last_sent"))]
    return sorted(act, key=lambda a: a.severity != "critical")


def plan_scan(snap: RunSnapshot, cfg: PagerConfig, state: PagerState, now: float) -> tuple[Outbox, PagerState]:
    st = PagerState.from_json(state.to_json())
    firing = {a.key: a for a in evaluate_rules(snap, cfg, st.baseline)}
    for key, a in firing.items():
        st.recovered.pop(key, None)
        e = st.open.setdefault(key, {"since": now, "fail": 0, "clear": 0, "last_sent": None})
        e.update(severity=a.severity, title=a.title, detail=a.detail, next_step=a.next_step, fail=e["fail"] + 1, clear=0)
    for key in [k for k in st.open if k not in firing]:
        e = st.open[key]
        e.update(fail=0, clear=e["clear"] + 1)
        if not e["last_sent"]:
            del st.open[key]
        elif e["clear"] >= cfg.scan.recover_after_scans:
            st.recovered[key] = {**e, "open_s": now - e["since"]}
            del st.open[key]
    st.pending = [k for k in st.pending if k in st.open]
    st.held = [k for k in st.held if k in st.open]
    st.baseline = {k: e["since"] for k, e in st.open.items()}
    due = []
    for key, e in st.open.items():
        crit = e["severity"] == "critical"
        if e["fail"] == 0 or (not crit and e["fail"] < cfg.scan.degraded_after_scans):
            continue
        cool = (cfg.alerts.critical_cooldown_minutes if crit else cfg.alerts.warning_cooldown_minutes) * 60
        if e["last_sent"] and now - e["last_sent"] < cool:
            continue
        if not _gate(cfg, st, now, crit):
            st.pending += [key] if key not in st.pending else []
            continue
        if not crit and _recent(st, now) + (1 if due else 0) >= cfg.alerts.max_texts_per_hour:
            st.held += [key] if key not in st.held else []
            continue
        due.append(_alert_of(key, e))
    msgs = []
    if due:
        due.sort(key=lambda a: a.severity != "critical")
        msgs.append(render_alert(due, snap, cfg, st, now))
    may_send = lambda: _gate(cfg, st, now, False) and _recent(st, now) + len(msgs) < cfg.alerts.max_texts_per_hour
    if not cfg.alerts.send_recovered:
        st.recovered = {}
    elif st.recovered and may_send():  # kept until a send succeeds (mark_sent), so quiet/mute/cap only delay it
        items = [(_alert_of(k, e), e["open_s"]) for k, e in st.recovered.items()]
        msgs.append(render_recovered(items, snap, cfg, st, now))
    for r in snap.runs:
        seen = st.runs_seen.get(r.run_id)
        event = "finished" if r.finished and seen != "finished" else ("started" if not seen else None)
        if event and may_send():  # runs_seen advances in mark_sent, so a gated event goes out later
            msgs.append(render_run_event(r, event, snap, cfg, st, now))
    return Outbox(msgs), st


def next_digest_at(cfg: PagerConfig, mode: str, now: float) -> float:
    step = cfg.digest_minutes(mode) * 60
    if step <= 0:
        return math.inf
    if not cfg.digest.align_to_clock:
        return now + step
    d = _local(now, cfg.timezone)
    midnight = d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return midnight + (math.floor((now - midnight) / step) + 1) * step


def plan_digest(snap, cfg: PagerConfig, state: PagerState, now: float) -> tuple[Outbox, PagerState]:
    """`snap` is a RunSnapshot or a zero-arg callable, taken only when a digest is rendered."""
    st, mode = PagerState.from_json(state.to_json()), cfg.run_mode if callable(snap) else snap.mode
    quiet, was_quiet = _in_quiet(cfg, now), st.in_quiet
    st.in_quiet = quiet
    if cfg.digest_minutes(mode) <= 0:
        return Outbox([]), st
    catch_up = was_quiet and not quiet and cfg.quiet_hours.catch_up_digest
    if st.next_digest is None and not catch_up:
        st.next_digest = next_digest_at(cfg, mode, now)
        return Outbox([]), st
    if not catch_up and now < st.next_digest:
        return Outbox([]), st
    st.next_digest = next_digest_at(cfg, mode, now)
    if not _gate(cfg, st, now, False) or _recent(st, now) >= cfg.alerts.max_texts_per_hour:
        return Outbox([]), st
    snap = snap() if callable(snap) else snap
    w, idle = snap.window or {}, False
    if not cfg.digest.when_idle and not w.get("received") and not _active(st, cfg) and \
            not any(not r.finished for r in snap.runs):
        beat = cfg.digest.idle_heartbeat_minutes * 60
        if not beat or (st.last_idle_digest and now - st.last_idle_digest < beat):
            return Outbox([]), st
        st.last_idle_digest, idle = now, True
    return Outbox([render_digest(snap, cfg, st, now, idle=idle)]), st


def mark_sent(state: PagerState, kind: str, alert_keys, now: float, ok: bool) -> PagerState:
    st = PagerState.from_json(state.to_json())
    if not ok:
        st.pending += [k for k in alert_keys if k not in st.pending and k in st.open]
        return st
    st.sent_log = [t for t in st.sent_log if now - t < 3600] + [now]
    for k in alert_keys:
        if k in st.open:
            st.open[k]["last_sent"] = now
        if kind == "resolved":
            st.recovered.pop(k, None)
        if kind.startswith("run_") and k.startswith("run:"):
            st.runs_seen[k[4:]] = kind[4:]
    st.pending = [k for k in st.pending if k not in alert_keys]
    st.held = [] if kind == "digest" else [k for k in st.held if k not in alert_keys]
    return st


# ── renderers ────────────────────────────────────────────────────────


def make_ref(now: float, seed: str, tz: str = "") -> str:
    return f"P-{_local(now, tz):%H%M}-{hashlib.sha1(seed.encode()).hexdigest()[:4]}"


def fit(lines: list[tuple[int, str]], max_chars: int) -> str:
    """Drop whole lines, lowest priority (>0) first, latest first among equals, until the text fits."""
    keep, dropped = list(lines), 0

    def text():
        body = [l for _, l in keep]
        if dropped:
            body.insert(len(body) - 1 if body and body[-1].startswith("Ref ") else len(body),
                        f"(+{dropped} more in pager log)")
        return "\n".join(body)

    while len(text()) > max_chars:
        cands = [i for i, (p, _) in enumerate(keep) if p > 0]
        if not cands:
            break
        del keep[min(cands, key=lambda i: (keep[i][0], -i))]
        dropped += 1
    return text()


def _pct(x) -> str:
    return f"{round(x * 100)}%"


def _verdict(alerts: list[Alert], snap: RunSnapshot, cfg: PagerConfig, state: PagerState, now: float) -> tuple[str, str]:
    top = alerts[0] if alerts else None
    if top and top.severity == "critical":
        status, line = "action", f"🔴 Action needed: {top.title}"
    elif top and top.key == "ingestion_paused":
        status, line = "paused", f"⏸ I{top.title[1:]}"
    elif top:
        status, line = "watch", f"🟠 Watch: {top.title}"
    elif snap.mode == "research" and any(not r.finished for r in snap.runs):
        status, line = "healthy", "🟢 Run on track — no action needed"
    else:
        status, line = "healthy", "🟢 All clear — no action needed"
    if state.muted_until and now < state.muted_until:
        line += f" (alerts muted until {_clock(state.muted_until, cfg.timezone)})"
    if state.held:
        line += f"; +{len(state.held)} held"
    return status, line + "."


def _problem(top: Alert | None, cfg: PagerConfig) -> list[tuple[int, str]]:
    if not (top and top.detail and top.since):
        return []
    return [(0, f"Since {_clock(top.since, cfg.timezone)}: {top.detail}.")]


def _also(others: list[Alert]) -> list[tuple[int, str]]:
    return [(5, "Also: " + "; ".join(a.title for a in others) + ".")] if others else []


def _models(snap: RunSnapshot) -> str:
    word = {"up": "up", "warm": "warm", "idle": "idle (scaled to zero)", "down": "DOWN",
            "degraded": "degraded", "unknown": "unknown"}
    groups: dict = {}
    for p in snap.providers:
        if p.name.startswith("modal:"):
            det = p.detail if p.state in ("up", "warm", "idle") else ""
            groups.setdefault((p.state, det), []).append(p.name[6:])
    parts = []
    for (state, det), names in groups.items():
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        w = "idle" if state == "idle" and det else word[state]
        parts.append(f"{who} {w}" + (f" ({det})" if det else ""))
    sents = ["Modal " + "; ".join(parts) + "."] if parts else []
    for p in snap.providers:
        if p.name.startswith("modal:"):
            continue
        s = f"{_display(p.name)} " + {"up": "OK", "warm": "OK", "down": "DOWN"}.get(p.state, word.get(p.state, p.state))
        left = (snap.openrouter or {}).get("limit_remaining") if p.name == "openrouter" else None
        sents.append(s + (f", ${left:.2f} credit left." if left is not None else "."))
    return "Models: " + " ".join(sents) if sents else ""


def _run_lines(r: RunProgress, now: float, tz: str) -> list[tuple[int, str]]:
    m, pct = r.metrics or {}, round(100 * r.done / r.total) if r.total else 0
    eta = _eta(r, now)
    line = f"{r.run_id} ({r.label}): {r.done} of {r.total} docs ({pct}%)"
    line += (f", ~{round(eta / 60)} min left" if eta is not None else "") + f", started {_clock(r.started_at, tz)}."
    stats = ([f"Accuracy {_pct(m['acc'])}"] if m.get("acc") is not None else []) + \
        ([f"review {_pct(m['review_rate'])}"] if m.get("review_rate") is not None else []) + \
        ([f"{m['failed']} failed"] if m.get("failed") is not None else [])
    money = ([f"${m['cost_usd']:.2f} so far" + (f" (${m['cost_usd'] / r.done:.3f}/doc)" if r.done else "")]
             if m.get("cost_usd") is not None else []) + \
        ([f"{round(m['p50_latency_s'])}s/doc (median)"] if m.get("p50_latency_s") is not None else [])
    return [(5, line)] + [(4, " · ".join(x) + ".") for x in (stats, money) if x]


def _eta(r: RunProgress, now: float) -> int | None:
    if r.finished or r.done < 3 or not r.total:
        return None
    return int((now - r.started_at) / r.done * (r.total - r.done))


def _finish(kind: str, lines: list[tuple[int, str]], snap, cfg, state, now, alerts_new=(), recovered=(), keys=()):
    head = f"Mailroom {lines[0][1]} · {_clock(now, cfg.timezone, zone=True)}"
    body = [(0, head)] + lines[1:]
    ref = make_ref(now, kind + "|" + snap.host + "|" + "\n".join(l for _, l in body), cfg.timezone)
    if cfg.machine.ref_in_text:
        body.append((0, f"Ref {ref}"))
    text = fit(body, cfg.max_chars)
    if cfg.style == "ascii":
        for k, v in ASCII.items():
            text = text.replace(k, v)
    payload = build_payload(kind, snap, cfg, state, now, ref=ref, alerts_new=alerts_new, recovered=recovered)
    payload["text"] = text
    return Message(kind, ref, text, payload, tuple(keys))


def _label(snap: RunSnapshot) -> str:
    return "RESEARCH" if snap.mode == "research" else "LIVE"


def render_digest(snap: RunSnapshot, cfg: PagerConfig, state: PagerState, now: float, idle: bool = False) -> Message:
    active = _active(state, cfg)
    others = [_alert_of(k, e) for k, e in state.open.items() if e["fail"] > 0 and (not active or k != active[0].key)]
    _, verdict = _verdict(active, snap, cfg, state, now)
    lines = [(0, _label(snap) + (" · IDLE" if idle else "")), (0, verdict), *_problem(active[0] if active else None, cfg)]
    lines += [(0, f"Next: {active[0].next_step}.")] if active and active[0].next_step else []
    lines += _also(others)
    live = [r for r in snap.runs if not r.finished]
    if snap.mode == "research" and live:
        for r in live[:2]:
            lines += _run_lines(r, now, cfg.timezone)
        lines += [(5, f"+{len(live) - 2} more runs (see pager log).")] if len(live) > 2 else []
    else:
        w, t, q = snap.window or {}, snap.today, snap.queues or {}
        if w:
            lines.append((4, (f"Last {w.get('minutes', 10)} min: {w.get('received', 0)} received, "
                              f"{w.get('archived', 0)} archived, {w.get('review', 0)} to review, "
                              f"{w.get('failed', 0)} failed.")))
            parts = (([f"{round(w['p50_latency_s'])}s/doc (median)"] if w.get("p50_latency_s") is not None else [])
                     + ([f"${w['cost_usd']:.2f}"] if w.get("cost_usd") is not None
                        and (w.get("archived") or w.get("review") or w.get("failed")) else [])
                     + ([f"{_pct(w['first_pass_rate'])} first-pass"] if w.get("first_pass_rate") is not None else []))
            lines += [(4, " · ".join(parts) + ".")] if parts else []
        if t:
            lines.append((3, (f"Today: {t.get('processed', 0)} processed · {t.get('review', 0)} to review · "
                              f"{t.get('failed', 0)} failed · ${t.get('cost_usd') or 0:.2f}.")))
        if q:
            lines.append((2, (f"Queues: {q.get('inbox', 0)} waiting, {q.get('processing', 0)} in progress, "
                              f"{q.get('review', 0)} awaiting review.")))
    models = _models(snap)
    lines += [(1, models)] if models else []
    return _finish("digest", lines, snap, cfg, state, now)


def render_alert(alerts: list[Alert], snap: RunSnapshot, cfg: PagerConfig, state: PagerState, now: float) -> Message:
    top, keys = alerts[0], [a.key for a in alerts]
    others = [_alert_of(k, e) for k, e in state.open.items() if e["fail"] > 0 and k != top.key]
    others += [a for a in alerts[1:] if a.key not in state.open]
    _, verdict = _verdict(alerts, snap, cfg, PagerState(muted_until=state.muted_until), now)
    lines = [(0, "ALERT"), (0, verdict), *_problem(top, cfg)]
    if snap.failures and top.key.startswith(("provider_down", "docs_failed", "watcher_down")):
        f = snap.failures[-1]
        lines.append((5, f"Latest: {f.get('name', '?')} — {f.get('error', 'unknown error')}."))
    lines += [(0, f"Next: {top.next_step}.")] if top.next_step else []
    lines += _also(others)
    return _finish("alert", lines, snap, cfg, state, now, alerts_new=alerts, keys=keys)


def _mid_sentence(phrase: str) -> str:
    """Lowercase a resolved phrase's first word unless it is a name (Modal, OpenRouter, Run …)."""
    return phrase[0].lower() + phrase[1:] if phrase.split(" ", 1)[0] in _COMMON_STARTS else phrase


_COMMON_STARTS = {"The", "Document", "No", "Ingestion", "Echo"}


def render_recovered(items: list[tuple[Alert, float]], snap: RunSnapshot, cfg: PagerConfig, state: PagerState,
                     now: float) -> Message:
    if len(items) == 1:
        a, s = items[0]
        line = f"✅ {_resolved_phrase(a.key)} after {_dur(s)}. No action needed."
    else:
        phrases = [_resolved_phrase(a.key) for a, _ in items]
        phrases = phrases[:1] + [_mid_sentence(p) for p in phrases[1:]]
        line = "✅ Resolved: " + "; ".join(f"{p} after {_dur(s)}" for p, (_, s) in zip(phrases, items, strict=True)) + \
               ". No action needed."
    return _finish("resolved", [(0, "RESOLVED"), (0, line)], snap, cfg, state, now,
                   recovered=items, keys=[a.key for a, _ in items])


def render_run_event(run: RunProgress, event: Literal["started", "finished"], snap: RunSnapshot, cfg: PagerConfig,
                     state: PagerState, now: float) -> Message:
    if event == "started":
        lines = [(0, f"▶ Run started: {run.run_id} · {run.total} docs · {run.label}.")]
    else:
        m = run.metrics or {}
        lines = [(0, f"✅ Run finished: {run.run_id} — {run.done} docs in {_dur(run.updated_at - run.started_at)}.")]
        stats = ([f"Accuracy {_pct(m['acc'])}"] if m.get("acc") is not None else []) + \
            ([f"review {_pct(m['review_rate'])}"] if m.get("review_rate") is not None else []) + \
            ([f"{m['failed']} failed"] if m.get("failed") is not None else []) + \
            ([f"${m['cost_usd']:.2f} total"] if m.get("cost_usd") is not None else [])
        lines += [(4, " · ".join(stats) + ".")] if stats else []
    return _finish(f"run_{event}", [(0, _label(snap))] + lines, snap, cfg, state, now, keys=[f"run:{run.run_id}"])


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def _alert_json(a: Alert) -> dict:
    return {"key": a.key, "severity": a.severity, "title": a.title, "detail": a.detail,
            "next_step": a.next_step, "since": _iso(a.since)}


def build_payload(kind, snap: RunSnapshot, cfg: PagerConfig, state: PagerState, now: float, *, ref: str,
                  alerts_new=(), recovered=()) -> dict:
    active = _active(state, cfg)
    status, _ = _verdict(sorted(list(alerts_new) + active, key=lambda a: a.severity != "critical"),
                         snap, cfg, state, now)
    w = snap.window or {}
    keys = ("received", "archived", "review", "failed", "p50_latency_s", "cost_usd", "first_pass_rate")
    return {
        "schema": SCHEMA, "ref": ref, "kind": kind, "mode": snap.mode, "sent_at": _iso(now),
        "host": snap.host, "repo": snap.repo, "status": status, "action_required": status == "action",
        "muted_until": _iso(state.muted_until) if state.muted_until and now < state.muted_until else None,
        "window": {"minutes": w.get("minutes", cfg.digest_minutes(snap.mode)), **{k: w.get(k) for k in keys}} if w else None,
        "today": dict(snap.today) if snap.today else None,
        "queues": dict(snap.queues) if snap.queues else None,
        "watcher": {"alive": snap.watcher_alive, "heartbeat_age_s": snap.heartbeat_age_s,
                    "uptime_s": snap.uptime_s, "sha": snap.sha},
        "providers": [{"key": p.name, "state": p.state, "detail": p.detail, "calls_10m": p.calls,
                       "errors_10m": p.errors} for p in snap.providers],
        "openrouter": dict(snap.openrouter) if snap.openrouter else None,
        "runs": [{"run_id": r.run_id, "label": r.label, "done": r.done, "total": r.total, "eta_s": _eta(r, now),
                  "started_at": _iso(r.started_at), "finished": r.finished, "metrics": dict(r.metrics or {})}
                 for r in snap.runs],
        "alerts": {"new": [_alert_json(a) for a in alerts_new], "open": [_alert_json(a) for a in active],
                   "recovered": [{"key": a.key, "open_s": round(s)} for a, s in recovered], "held": len(state.held)},
        "text": "", "delivery": [],
    }
