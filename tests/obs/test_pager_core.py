"""Operator SMS pager core: config resolution, frequency state machine, SMS
renderers and the machine payload. Pure — every test passes `now`."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mailroom_reloaded.obs.pager_core import (
    Alert,
    PagerConfig,
    PagerState,
    ProviderStatus,
    RunProgress,
    RunSnapshot,
    build_payload,
    evaluate_rules,
    fit,
    make_ref,
    mark_sent,
    next_digest_at,
    normalize_number,
    plan_digest,
    plan_scan,
    render_alert,
    render_digest,
    render_recovered,
    render_run_event,
)

TZ = ZoneInfo("America/Chicago")


def at(hhmm: str, day: int = 15) -> float:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


def cfg(env=None, environment="live", **block):
    base = {"timezone": "America/Chicago"}
    for k, v in block.items():
        if isinstance(v, dict):
            base.setdefault(k, {}).update(v)
        else:
            base[k] = v
    return PagerConfig.resolve(base, env or {}, environment=environment)


PROVIDERS = [ProviderStatus("modal:fast", "up"), ProviderStatus("modal:extract", "idle"),
             ProviderStatus("modal:vision", "idle"), ProviderStatus("openrouter", "up")]


def snap(now=None, **kw) -> RunSnapshot:
    d = dict(  # noqa: C408 — keyword form mirrors RunSnapshot fields
        taken_at=now or at("14:20"), mode="production", watcher_alive=True, heartbeat_age_s=1.2,
        uptime_s=11520.0, sha="a1b2c3d", queues={"inbox": 3, "processing": 1, "review": 6, "failed": 2},
        window={"minutes": 10, "received": 14, "archived": 11, "review": 2, "failed": 0,
                "p50_latency_s": 48.0, "cost_usd": 0.21, "first_pass_rate": 0.79},
        stuck=0, paused=None, outbox_dead=0, providers=list(PROVIDERS), runs=[], failures=[],
        openrouter={"limit_remaining": 4.12, "usage": 15.88, "is_free_tier": False},
        today={"processed": 312, "review": 9, "failed": 4, "cost_usd": 4.86}, host="mac", repo="llm-mailroom")
    d.update(kw)
    return RunSnapshot(**d)


def down_snap(now, **kw):
    provs = [ProviderStatus("modal:extract", "down", "HTTP 502", calls=5, errors=5),
             ProviderStatus("modal:fast", "up"), ProviderStatus("openrouter", "up")]
    fails = [{"name": f"claim_00{n}.pdf", "error": "extraction timed out", "at": at("14:20") + n}
             for n in (40, 41, 42)]
    kw.setdefault("queues", {"inbox": 7, "processing": 0, "review": 22, "failed": 5})
    kw.setdefault("window", {"minutes": 10, "received": 9, "archived": 4, "review": 1, "failed": 3})
    return snap(now, providers=provs, failures=fails, openrouter=None, today=None, **kw)


def scan(c, s, state, now, ok=True):
    out, st = plan_scan(s, c, state, now)
    for m in out.messages:
        st = mark_sent(st, m.kind, m.alert_keys, now, ok)
    return out, st


def with_ref(expected: str, msg) -> str:
    assert re.fullmatch(r"P-\d{4}-[0-9a-f]{4}", msg.ref)
    return expected.replace("Ref P-XXXX", f"Ref {msg.ref}")


# ── config ───────────────────────────────────────────────────────────


def test_resolve_precedence_env_over_taxonomy_over_default():
    c = PagerConfig.resolve({"style": "ascii", "scan": {"seconds": 45}},
                            {"MAILROOM_PAGER_SCAN_SECONDS": "30", "MAILROOM_PAGER_TO": "608-555-1234"},
                            environment="live")
    assert c.style == "ascii" and c.scan.seconds == 30 and c.transport == "twilio"
    assert c.recipients == ("+16085551234",)
    assert c.sources["style"] == "taxonomy" and c.sources["scan.seconds"] == "env"
    assert c.sources["transport"] == "default" and c.sources["recipients"] == "env"


def test_settings_file_wins_for_enabled_and_recipients():
    c = PagerConfig.resolve({"enabled": True}, {"MAILROOM_PAGER": "1"}, environment="live",
                            settings={"enabled": False, "recipients": ["+16085551234"]})
    assert c.enabled is False and c.recipients == ("+16085551234",)
    assert c.sources["enabled"] == "settings"


def test_default_is_off():
    assert PagerConfig.resolve(None, {}, environment="live").enabled is False


def test_normalize_number():
    assert normalize_number("608-555-1234") == normalize_number("(608) 555 1234") == "+16085551234"
    assert normalize_number("1.608.555.1234") == "+16085551234"
    assert normalize_number("+44 20 7946 0958") == "+442079460958"
    assert normalize_number("12345") is None and normalize_number("call me") is None


def test_resolve_floors_and_bad_values():
    c = PagerConfig.resolve({"scan": {"seconds": 2}, "max_chars": 50, "digest": {"production_minutes": -5},
                             "transport": "pigeon", "bogus": 1},
                            {"MAILROOM_PAGER_DIGEST_MINUTES": "abc"}, environment="live")
    assert c.scan.seconds == 15 and c.max_chars == 160 and c.transport == "twilio"
    assert c.digest.research_minutes == 10 and c.digest.production_minutes == 0
    assert any("digest" in w for w in c.warnings)
    assert any("transport" in w for w in c.warnings) and any("bogus" in w for w in c.warnings)


def test_quiet_window_parse_cross_midnight():
    c = cfg(quiet_hours={"window": "22:00-07:00"})
    assert [t.strftime("%H:%M") for t in c.quiet] == ["22:00", "07:00"]
    assert cfg(quiet_hours={"window": "nope"}).quiet is None and cfg().quiet is None


def test_mode_auto():
    assert cfg(environment="pilot").run_mode == cfg(environment="eval").run_mode == "research"
    assert cfg(environment="live").run_mode == "production"
    assert cfg(environment="pilot", mode="production").run_mode == "production"
    assert cfg().digest_minutes("research") == 10


# ── frequency state machine ──────────────────────────────────────────


def test_healthy_scan_is_silent():
    c, st = cfg(), PagerState()
    out, st2 = plan_scan(snap(), c, st, at("14:20"))
    assert out.messages == [] and st2.open == {} and st2.sent_log == [] and st2.pending == []


def test_critical_texts_first_scan_warning_after_debounce():
    c, st = cfg(), PagerState()
    s = snap(queues={"inbox": 0, "processing": 0, "review": 25, "failed": 0})
    out, st = scan(c, s, st, at("14:20"))
    assert out.messages == []                       # warning: 1 of 2 scans
    out, st = scan(c, s, st, at("14:21"))
    assert [m.alert_keys for m in out.messages] == [("review_backlog",)]
    out, _ = scan(c, snap(watcher_alive=False, heartbeat_age_s=240), PagerState(), at("14:20"))
    assert out.messages[0].alert_keys == ("watcher_down",)


def test_cooldowns_per_severity():
    c, st = cfg(), PagerState()
    t0 = at("14:00")
    out, st = scan(c, down_snap(t0), st, t0)
    assert "provider_down:modal:extract" in out.messages[0].alert_keys
    out, st = scan(c, down_snap(t0 + 600), st, t0 + 600)
    assert not any("provider_down:modal:extract" in m.alert_keys for m in out.messages)
    out, st = scan(c, down_snap(t0 + 900), st, t0 + 900)
    assert any("provider_down:modal:extract" in m.alert_keys for m in out.messages)
    w = snap(queues={"inbox": 0, "processing": 0, "review": 25, "failed": 0})
    st = PagerState()
    _, st = scan(c, w, st, t0)
    out, st = scan(c, w, st, t0 + 60)
    assert out.messages
    out, st = scan(c, w, st, t0 + 60 + 50 * 60)
    assert out.messages == []
    out, st = scan(c, w, st, t0 + 60 + 61 * 60)
    assert out.messages


def test_flapping_key_does_not_spam():
    c, st, sent = cfg(), PagerState(), []
    for i in range(8):
        s = down_snap(at("14:00") + 60 * i) if i % 2 == 0 else snap(at("14:00") + 60 * i)
        out, st = scan(c, s, st, at("14:00") + 60 * i)
        sent += [m.kind for m in out.messages if "provider_down:modal:extract" in m.alert_keys
                 or m.kind == "resolved"]
    assert sent == ["alert"]


def test_recovered_batched_after_n_clear_scans():
    c, st = cfg(), PagerState()
    _, st = scan(c, down_snap(at("14:23"), watcher_alive=False, heartbeat_age_s=300), st, at("14:23"))
    out, st = scan(c, snap(at("14:40")), st, at("14:40"))
    assert out.messages == []
    out, st = scan(c, snap(at("14:41")), st, at("14:41"))
    assert [m.kind for m in out.messages] == ["resolved"]
    assert set(out.messages[0].alert_keys) >= {"provider_down:modal:extract", "watcher_down"}
    assert st.open == {}


def test_quiet_hours_cross_midnight_and_catch_up():
    c = cfg(quiet_hours={"window": "22:00-07:00"})
    st = PagerState(next_digest=at("06:50", 16))
    out, st = plan_digest(snap(at("06:50", 16)), c, st, at("06:50", 16))
    assert out.messages == [] and st.in_quiet
    out, _ = scan(c, down_snap(at("23:30")), PagerState(), at("23:30"))      # critical still texts
    assert [m.kind for m in out.messages] == ["alert"]
    w = snap(at("23:30"), queues={"inbox": 0, "processing": 0, "review": 25, "failed": 0})
    st_w = PagerState()
    for t in ("23:30", "23:31", "23:32"):
        out, st_w = scan(c, w, st_w, at(t))
        assert out.messages == []
    assert "review_backlog" in st_w.pending
    out, st = plan_digest(snap(at("07:00", 16)), c, st, at("07:00", 16))
    assert [m.kind for m in out.messages] == ["digest"]
    out, st = plan_digest(snap(at("07:00", 16) + 30), c, st, at("07:00", 16) + 30)
    assert out.messages == []
    out, _ = scan(c, w, st_w, at("07:01", 16))                               # pending goes out
    assert [m.alert_keys for m in out.messages] == [("review_backlog",)]


def test_mute_blocks_all_but_critical():
    c = cfg()
    w = snap(queues={"inbox": 0, "processing": 0, "review": 25, "failed": 0})
    st = PagerState(muted_until=at("16:20"))
    for t in ("14:20", "14:21"):
        out, st = scan(c, w, st, at(t))
        assert out.messages == []
    out, _ = scan(c, down_snap(at("14:22")), PagerState(muted_until=at("16:20")), at("14:22"))
    assert out.messages and out.messages[0].kind == "alert"
    out, _ = plan_digest(snap(), c, PagerState(next_digest=at("14:20"), muted_until=at("16:20")), at("14:20"))
    assert out.messages == []
    m = render_digest(snap(), c, PagerState(muted_until=at("16:20")), at("14:20"))
    assert "🟢 All clear — no action needed (alerts muted until 4:20 PM)." in m.text


def test_cap_lets_criticals_through_and_holds_rest():
    c = cfg(alerts={"max_texts_per_hour": 2})
    st = PagerState(sent_log=[at("14:00"), at("14:05")])
    w = down_snap(at("14:20"), watcher_alive=True)
    _, st = plan_scan(w, c, st, at("14:19"))
    out, st = plan_scan(w, c, st, at("14:20"))
    keys = [k for m in out.messages for k in m.alert_keys]
    assert "provider_down:modal:extract" in keys and "review_backlog" not in keys
    assert "review_backlog" in st.held
    out, _ = plan_digest(snap(), c, PagerState(next_digest=at("14:20"), sent_log=st.sent_log * 3), at("14:20"))
    assert out.messages == []
    held = PagerState(held=["review_backlog"], open={"review_backlog": {
        "severity": "warning", "title": "review queue at 22 (limit 20)", "detail": "", "next_step": "x",
        "since": at("14:00"), "fail": 3, "clear": 0, "last_sent": None}})
    assert "🟠 Watch: review queue at 22 (limit 20); +1 held." in render_digest(snap(), c, held, at("14:30")).text


def test_each_successful_send_counts_toward_the_cap_but_criticals_do_not():
    st = PagerState(open={"review_backlog": {"severity": "warning"}, "watcher_down": {"severity": "critical"}})
    part = mark_sent(st, "alert", ("review_backlog",), at("14:20"), False, sends=1)  # partial: counted, not acked
    assert part.sent_log == [at("14:20")] and part.pending == ["review_backlog"]
    assert mark_sent(st, "alert", ("review_backlog",), at("14:20"), True, sends=2).sent_log == [at("14:20")] * 2
    assert mark_sent(st, "alert", ("watcher_down",), at("14:20"), True, sends=2).sent_log == []


def test_digest_aligned_to_clock_and_custom_cadence():
    assert next_digest_at(cfg(), "production", at("14:13")) == at("14:20")
    assert next_digest_at(cfg(), "production", at("14:20")) == at("14:30")
    c15 = cfg(digest={"production_minutes": 15})
    assert next_digest_at(c15, "production", at("14:13")) == at("14:15")
    assert next_digest_at(c15, "production", at("14:15")) == at("14:30")
    free = cfg(digest={"align_to_clock": False})
    assert next_digest_at(free, "production", at("14:13") + 7) == at("14:23") + 7
    st = PagerState()
    out, st = plan_digest(snap(), cfg(), st, at("14:13"))
    assert out.messages == [] and st.next_digest == at("14:20")
    out, st = plan_digest(snap(), cfg(), st, at("14:20"))
    assert [m.kind for m in out.messages] == ["digest"] and st.next_digest == at("14:30")


def test_digest_disabled_when_minutes_zero():
    c = cfg(digest={"production_minutes": 0})
    st = PagerState()
    for t in ("14:00", "14:10", "15:00"):
        out, st = plan_digest(snap(), c, st, at(t))
        assert out.messages == []


def test_idle_suppressed_except_heartbeat():
    c = cfg(digest={"when_idle": False, "idle_heartbeat_minutes": 60})
    idle = snap(window={"minutes": 10, "received": 0, "archived": 0, "review": 0, "failed": 0})
    st = PagerState(next_digest=at("14:20"))
    out, st = plan_digest(idle, c, st, at("14:20"))
    assert len(out.messages) == 1 and "Mailroom LIVE · IDLE · 2:20 PM CDT" in out.messages[0].text
    st = mark_sent(st, "digest", (), at("14:20"), True)
    for t in ("14:30", "14:40", "15:10"):
        out, st = plan_digest(idle, c, st, at(t))
        assert out.messages == []
    out, _ = plan_digest(idle, c, st, at("15:20"))
    assert len(out.messages) == 1
    out, _ = plan_digest(snap(), c, PagerState(next_digest=at("14:20")), at("14:20"))
    assert len(out.messages) == 1 and "IDLE" not in out.messages[0].text


RULE_CASES = [
    ("watcher_down", {"watcher_alive": False, "heartbeat_age_s": 400}, "watcher_down", "critical"),
    ("provider_down", {"providers": [ProviderStatus("openrouter", "down", "HTTP 401")]}, "provider_down:openrouter", "critical"),
    ("provider_degraded", {"providers": [ProviderStatus("modal:fast", "up", calls=8, errors=3)]}, "provider_degraded:modal:fast", "warning"),
    ("openrouter_credit", {"openrouter": {"limit_remaining": 0.5, "usage": 1, "is_free_tier": False}}, "openrouter_credit", "warning"),
    ("docs_failed", {"window": {"minutes": 10, "failed": 2}}, "docs_failed", "warning"),
    ("review_backlog", {"queues": {"review": 21}}, "review_backlog", "warning"),
    ("stuck_docs", {"stuck": 2}, "stuck_docs", "warning"),
    ("ingestion_paused", {"paused": {"actor": "ops", "reason": "maintenance"}}, "ingestion_paused", "warning"),
    ("outbox_dead", {"outbox_dead": 3}, "outbox_dead", "warning"),
    ("run_stalled", {"runs": [RunProgress("r1", "run_pilot", 10, 3, at("13:00"), at("14:00"), False, {})]}, "run_stalled:r1", "warning"),
]


@pytest.mark.parametrize("rule,kw,key,severity", RULE_CASES)
def test_each_rule_toggle_and_threshold(rule, kw, key, severity):
    on = {a.key: a for a in evaluate_rules(snap(**kw), cfg(), {})}
    assert key in on and on[key].severity == severity and on[key].title and on[key].next_step
    off = evaluate_rules(snap(**kw), cfg(rules={rule: {"enabled": False}}), {})
    assert key not in {a.key for a in off}
    assert set(cfg().rules) == {r for r, *_ in RULE_CASES}


def test_rule_thresholds():
    assert evaluate_rules(snap(window={"minutes": 10, "failed": 5}), cfg(), {})[0].severity == "critical"
    assert evaluate_rules(snap(queues={"review": 20}), cfg(), {}) == []
    assert evaluate_rules(snap(queues={"review": 21}), cfg(rules={"review_backlog": {"threshold": 30}}), {}) == []
    few = [ProviderStatus("modal:fast", "up", calls=3, errors=3)]
    assert evaluate_rules(snap(providers=few), cfg(), {}) == []
    assert evaluate_rules(snap(outbox_dead=None), cfg(), {}) == []
    run = RunProgress("r1", "x", 10, 3, at("14:00"), at("14:10"), False, {})
    assert evaluate_rules(snap(runs=[run]), cfg(), {}) == []           # 10 min < 15


# ── goldens (Message design samples) ─────────────────────────────────


def test_golden_production_healthy():
    s = snap(window=dict(snap().window, failed=1))
    m = render_digest(s, cfg(), PagerState(), at("14:20"))
    assert m.text == with_ref("""Mailroom LIVE · 2:20 PM CDT
🟢 All clear — no action needed.
Last 10 min: 14 received, 11 archived, 2 to review, 1 failed.
48s/doc (median) · $0.21 · 79% first-pass.
Today: 312 processed · 9 to review · 4 failed · $4.86.
Queues: 3 waiting, 1 in progress, 6 awaiting review.
Models: Modal fast up; extract and vision idle (scaled to zero). OpenRouter OK, $4.12 credit left.
Ref P-XXXX""", m) and m.ref.startswith("P-1420-")


def _incident_state():
    _, st = scan(cfg(), down_snap(at("14:23")), PagerState(), at("14:23"))
    return st


def test_golden_production_with_open_alert():
    m = render_digest(down_snap(at("14:30")), cfg(), _incident_state(), at("14:30"))
    assert m.text == with_ref("""Mailroom LIVE · 2:30 PM CDT
🔴 Action needed: Modal extract tier is down.
Since 2:23 PM: 5 of 5 calls failed (HTTP 502).
Next: check `modal app logs mailroom-vllm`.
Also: 3 documents failed since 2:20 PM; review queue at 22 (limit 20).
Last 10 min: 9 received, 4 archived, 1 to review, 3 failed.
Queues: 7 waiting, 0 in progress, 22 awaiting review.
Models: Modal extract DOWN; fast up. OpenRouter OK.
Ref P-XXXX""", m)


RUN = RunProgress("pilot-real-1019", "run_pilot --real", 100, 58, at("13:42"), at("14:19"), False,
                  {"acc": 0.86, "review_rate": 0.06, "failed": 1, "cost_usd": 1.92, "p50_latency_s": 41.0})
RESEARCH_PROVIDERS = [ProviderStatus("modal:extract", "warm", "1 container, 2 cold-start retries"),
                      ProviderStatus("openrouter", "up")]


def test_golden_research_digest():
    s = snap(mode="research", runs=[RUN], providers=RESEARCH_PROVIDERS)
    m = render_digest(s, cfg(environment="pilot"), PagerState(), at("14:20"))
    assert m.text == with_ref("""Mailroom RESEARCH · 2:20 PM CDT
🟢 Run on track — no action needed.
pilot-real-1019 (run_pilot --real): 58 of 100 docs (58%), ~28 min left, started 1:42 PM.
Accuracy 86% · review 6% · 1 failed.
$1.92 so far ($0.033/doc) · 41s/doc (median).
Models: Modal extract warm (1 container, 2 cold-start retries). OpenRouter OK, $4.12 credit left.
Ref P-XXXX""", m)


def test_golden_alert():
    out, _ = plan_scan(down_snap(at("14:23")), cfg(), PagerState(), at("14:23"))
    (m,) = out.messages
    assert m.text == with_ref("""Mailroom ALERT · 2:23 PM CDT
🔴 Action needed: Modal extract tier is down.
Since 2:23 PM: 5 of 5 calls failed (HTTP 502).
Latest: claim_0042.pdf — extraction timed out.
Next: check `modal app logs mailroom-vllm`.
Also: 3 documents failed since 2:20 PM; review queue at 22 (limit 20).
Ref P-XXXX""", m)


def test_golden_resolved():
    st = _incident_state()
    _, st = scan(cfg(), snap(at("14:40")), st, at("14:40"))
    out, _ = scan(cfg(), snap(at("14:41")), st, at("14:41"))
    (m,) = out.messages
    assert m.text == with_ref("""Mailroom RESOLVED · 2:41 PM CDT
✅ Modal extract tier is back up after 18 min. No action needed.
Ref P-XXXX""", m)


def test_golden_run_started():
    run = RunProgress("pilot-real-1019", "run_pilot --real", 100, 0, at("13:42"), at("13:42"), False, {})
    m = render_run_event(run, "started", snap(mode="research"), cfg(), PagerState(), at("13:42"))
    assert m.text == with_ref("""Mailroom RESEARCH · 1:42 PM CDT
▶ Run started: pilot-real-1019 · 100 docs · run_pilot --real.
Ref P-XXXX""", m)
    out, st = scan(cfg(), snap(at("13:42"), mode="research", runs=[run]), PagerState(), at("13:42"))
    assert [m.kind for m in out.messages] == ["run_started"]
    out, _ = scan(cfg(), snap(at("13:43"), mode="research", runs=[run]), st, at("13:43"))
    assert out.messages == []


def test_golden_run_finished():
    run = RunProgress("pilot-real-1019", "run_pilot --real", 100, 100, at("14:42"), at("15:46"), True,
                      {"acc": 0.88, "review_rate": 0.05, "failed": 1, "cost_usd": 3.30})
    m = render_run_event(run, "finished", snap(mode="research"), cfg(), PagerState(), at("15:46"))
    assert m.text == with_ref("""Mailroom RESEARCH · 3:46 PM CDT
✅ Run finished: pilot-real-1019 — 100 docs in 1h 04m.
Accuracy 88% · review 5% · 1 failed · $3.30 total.
Ref P-XXXX""", m)


# ── machine payload ──────────────────────────────────────────────────

PAYLOAD_KEYS = {"schema", "ref", "kind", "mode", "sent_at", "host", "repo", "status", "action_required",
                "muted_until", "window", "today", "queues", "watcher", "providers", "openrouter", "runs",
                "alerts", "text", "delivery"}


def test_payload_schema_and_text_match():
    healthy = render_digest(snap(), cfg(), PagerState(), at("14:20"))
    incident = render_digest(down_snap(at("14:30")), cfg(), _incident_state(), at("14:30"))
    for m, status in ((healthy, "healthy"), (incident, "action")):
        p = m.payload
        assert set(p) == PAYLOAD_KEYS and p["schema"] == "mailroom.pager/v1"
        assert p["text"] == m.text and p["ref"] == m.ref and p["delivery"] == []
        assert p["status"] == status and p["action_required"] is (status == "action")
        json.dumps(p)
    p = incident.payload
    assert p["providers"][0] == {"key": "modal:extract", "state": "down", "detail": "HTTP 502",
                                 "calls_10m": 5, "errors_10m": 5}
    assert p["alerts"]["open"][0]["key"] == "provider_down:modal:extract"
    assert p["sent_at"] == "2026-10-15T19:30:00Z" and p["kind"] == "digest" and p["mode"] == "production"
    r = render_digest(snap(mode="research", runs=[RUN]), cfg(), PagerState(), at("14:20")).payload
    assert r["runs"][0]["eta_s"] == 1651 and r["runs"][0]["metrics"]["acc"] == 0.86


def test_payload_null_when_source_missing():
    p = render_digest(snap(openrouter=None, today=None, window={}), cfg(), PagerState(), at("14:20")).payload
    assert p["openrouter"] is None and p["today"] is None and p["window"] is None


def test_human_text_has_no_json_or_keys():
    msgs = [render_digest(snap(), cfg(), PagerState(), at("14:20")),
            render_digest(down_snap(at("14:30")), cfg(), _incident_state(), at("14:30"))]
    out, _ = plan_scan(down_snap(at("14:23")), cfg(), PagerState(), at("14:23"))
    for m in msgs + out.messages:
        assert "{" not in m.text and '"schema"' not in m.text and "mailroom.pager" not in m.text
        assert "provider_down" not in m.text


def test_needs_to_know_precedes_metrics():
    lines = render_digest(down_snap(at("14:30")), cfg(), _incident_state(), at("14:30")).text.splitlines()
    i_next = next(i for i, l in enumerate(lines) if l.startswith("Next:"))
    i_last = next(i for i, l in enumerate(lines) if l.startswith("Last 10 min"))
    assert lines[1].startswith("🔴 Action needed") and i_next < i_last


def test_ref_in_text_toggle():
    m = render_digest(snap(), cfg(machine={"ref_in_text": False}), PagerState(), at("14:20"))
    assert "Ref " not in m.text and m.ref and m.payload["ref"] == m.ref


GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
           "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")


def test_ascii_style_is_gsm7():
    c = cfg(style="ascii")
    msgs = [render_digest(snap(), c, PagerState(), at("14:20")),
            render_digest(down_snap(at("14:30")), c, _incident_state(), at("14:30"))]
    out, _ = plan_scan(down_snap(at("14:23")), c, PagerState(), at("14:23"))
    for m in msgs + out.messages:
        assert set(m.text) <= GSM7, set(m.text) - GSM7
    assert msgs[0].text.splitlines()[1] == "OK: All clear - no action needed."
    assert msgs[1].text.splitlines()[1] == "ACTION: Modal extract tier is down."


def test_fit_drops_lowest_priority_whole_lines():
    lines = [(0, "Header"), (0, "Verdict"), (4, "W" * 50), (3, "T" * 50), (2, "Q" * 50), (1, "M" * 50), (0, "Ref P-1")]
    assert fit(lines, 1000) == "\n".join(l for _, l in lines)
    out = fit(lines, 150)
    assert out.splitlines() == ["Header", "Verdict", "W" * 50, "T" * 50, "(+2 more in pager log)", "Ref P-1"]
    assert len(out) <= 150
    assert fit([(0, "X" * 300)], 160) == "X" * 300            # priority 0 never dropped
    m = render_digest(snap(), cfg(max_chars=200), PagerState(), at("14:20"))
    assert len(m.text) <= 200 and "more in pager log" in m.text and "Models:" not in m.text


def test_make_ref_deterministic():
    assert make_ref(at("14:20"), "seed", "America/Chicago") == make_ref(at("14:20"), "seed", "America/Chicago")
    assert re.fullmatch(r"P-1420-[0-9a-f]{4}", make_ref(at("14:20"), "seed", "America/Chicago"))


def test_state_json_roundtrip():
    st = _incident_state()
    st.held.append("review_backlog")
    st.runs_seen["r1"] = "started"
    back = PagerState.from_json(json.loads(json.dumps(st.to_json())))
    assert back == st
    assert PagerState.from_json({}) == PagerState()


def test_render_alert_and_recovered_direct():
    a = Alert("outbox_dead", "warning", "2 echo emails could not be delivered", next_step="check the Gmail channel credentials")
    m = render_alert([a], snap(), cfg(), PagerState(), at("14:20"))
    assert m.kind == "alert" and "🟠 Watch: 2 echo emails could not be delivered." in m.text
    r = render_recovered([(a, 300.0)], snap(), cfg(), PagerState(), at("14:20"))
    assert r.kind == "resolved" and "after 5 min" in r.text
    p = build_payload("alert", snap(), cfg(), PagerState(), at("14:20"), ref="P-1420-0000", alerts_new=[a])
    assert p["alerts"]["new"][0]["key"] == "outbox_dead" and p["status"] == "watch"


def test_empty_window_has_no_metrics_line():
    empty = {"minutes": 10, "received": 0, "archived": 0, "review": 0, "failed": 0, "p50_latency_s": None,
             "cost_usd": 0.0, "first_pass_rate": None}
    text = render_digest(snap(window=empty), cfg(), PagerState(), at("14:20")).text
    assert "$0.00." not in text.splitlines()[3] and "Last 10 min: 0 received" in text


    assert "$0.00." not in text.splitlines()[3] and "Last 10 min: 0 received" in text


def test_watcher_down_without_heartbeat_has_no_unknown_age():
    s = snap(watcher_alive=False, heartbeat_age_s=None, repo="mailroom-reloaded")
    [a] = [a for a in evaluate_rules(s, cfg(), {}) if a.key == "watcher_down"]
    assert a.title == "the watcher has stopped (no watcher process is running)"
    assert "mailroom watch" in a.next_step


def test_batched_resolved_lowercases_mid_sentence_phrases():
    now = at("14:37")
    items = [(Alert("provider_down:modal:extract", "critical", "x"), 840.0),
             (Alert("docs_failed", "warning", "y"), 840.0), (Alert("watcher_down", "critical", "z"), 60.0)]
    msg = render_recovered(items, snap(now), cfg(), PagerState(), now)
    assert ("✅ Resolved: Modal extract tier is back up after 14 min; document failures have stopped after 14 min; "
            "the watcher is running again after 1 min. No action needed.") in msg.text


# ── final-review fixes ───────────────────────────────────────────────


def test_recovered_text_waits_out_quiet_hours_instead_of_vanishing():
    c = cfg(quiet_hours={"window": "22:00-07:00"})
    _, st = scan(c, down_snap(at("23:30")), PagerState(), at("23:30"))
    assert st.open["provider_down:modal:extract"]["last_sent"]
    sent = []
    for t in (at("23:36"), at("23:37"), at("23:38")):
        out, st = scan(c, snap(t), st, t)
        sent += out.messages
    assert sent == []  # recovery is not critical: held through quiet hours
    out, st = scan(c, snap(at("07:01", day=16)), st, at("07:01", day=16))
    assert [m.kind for m in out.messages] == ["resolved"]
    assert "back up" in out.messages[0].text
    out, _ = scan(c, snap(at("07:02", day=16)), st, at("07:02", day=16))
    assert out.messages == []  # sent once


def test_recovered_text_retried_after_failed_send():
    c = cfg()
    _, st = scan(c, down_snap(at("14:00")), PagerState(), at("14:00"))
    out, st = scan(c, snap(at("14:01")), st, at("14:01"))
    out, st = scan(c, snap(at("14:02")), st, at("14:02"), ok=False)
    assert [m.kind for m in out.messages] == ["resolved"]
    out, st = scan(c, snap(at("14:03")), st, at("14:03"))
    assert [m.kind for m in out.messages] == ["resolved"]
    out, _ = scan(c, snap(at("14:04")), st, at("14:04"))
    assert out.messages == []


def test_run_event_not_lost_while_gated():
    c = cfg(quiet_hours={"window": "22:00-07:00"})
    run = RunProgress("eval-1", "eval", 50, 0, at("23:00"), at("23:00"), False, {})
    out, st = scan(c, snap(at("23:00"), mode="research", runs=[run]), PagerState(), at("23:00"))
    assert out.messages == []
    t = at("07:00", day=16)
    run = RunProgress("eval-1", "eval", 50, 20, at("23:00"), t, False, {})  # still progressing
    out, st = scan(c, snap(t, mode="research", runs=[run]), st, t)
    assert [m.kind for m in out.messages] == ["run_started"]
    out, _ = scan(c, snap(t + 60, mode="research", runs=[run]), st, t + 60)
    assert out.messages == []


def test_invalid_timezone_falls_back_and_warns():
    c = cfg(timezone="Mars/Base")
    assert c.timezone == "" and any("timezone" in w for w in c.warnings)
    out, _ = plan_digest(snap(at("14:20")), c, PagerState(next_digest=at("14:10")), at("14:20"))
    assert len(out.messages) == 1


def test_non_mapping_pager_block_is_ignored_with_warning():
    for block in ("x", ["x"], 3):
        c = PagerConfig.resolve(block, {}, environment="live")
        assert c.enabled is False and any("pager_config_invalid" in w for w in c.warnings)


def test_email_gateway_accepts_gateway_addresses():
    c = PagerConfig.resolve({"transport": "email_gateway", "enabled": True,
                             "recipients": ["6085551234@VText.com", "+16085551234"]}, {}, environment="live")
    assert c.recipients == ("6085551234@vtext.com",) and c.active
    tw = PagerConfig.resolve({"recipients": ["6085551234@vtext.com", "608-555-1234"]}, {}, environment="live")
    assert tw.recipients == ("+16085551234",)
    from mailroom_reloaded.obs.pager_core import normalize_recipient
    assert normalize_recipient("6085551234@vtext.com", "email_gateway") == "6085551234@vtext.com"
    assert normalize_recipient("6085551234@vtext.com", "twilio") is None
    assert normalize_recipient("(608) 555-1234", "twilio") == "+16085551234"


def test_watcher_down_is_not_raised_in_research_mode():
    keys = [a.key for a in evaluate_rules(snap(mode="research", watcher_alive=False), cfg(), {})]
    assert "watcher_down" not in keys
    assert "watcher_down" in [a.key for a in evaluate_rules(snap(watcher_alive=False), cfg(), {})]


def test_apply_settings_overrides_explicit_config():
    c = cfg(enabled=True, recipients=["+16085551234"])
    off = c.apply_settings({"enabled": False})
    assert off.enabled is False and off.recipients == c.recipients
    moved = c.apply_settings({"recipients": ["414-555-9876", "junk"]})
    assert moved.recipients == ("+14145559876",) and moved.enabled


def test_plan_digest_takes_snapshot_only_when_rendering():
    calls = []

    def lazy():
        calls.append(1)
        return snap(at("14:20"))
    c = cfg(quiet_hours={"window": "22:00-07:00"})
    plan_digest(lazy, c, PagerState(next_digest=at("14:30")), at("14:20"))
    plan_digest(lazy, c, PagerState(in_quiet=True, next_digest=at("23:10")), at("23:05"))
    assert calls == []
    out, _ = plan_digest(lazy, c, PagerState(next_digest=at("14:10")), at("14:20"))
    assert calls == [1] and len(out.messages) == 1


def test_core_is_verbatim_port():
    from pathlib import Path

    here = Path(__file__).resolve().parents[2] / "src/mailroom_reloaded/obs/pager_core.py"
    upstream = Path(__file__).resolve().parents[3] / "llm-mailroom/src/pipeline/pager_core.py"
    if not upstream.exists():
        pytest.skip("llm-mailroom checkout not next to this one")
    assert here.read_text().splitlines()[1:] == upstream.read_text().splitlines()[1:]


def test_cap_of_one_still_lets_one_warning_through():
    c = cfg(alerts={"max_texts_per_hour": 1})
    w = snap(at("14:20"), queues={"inbox": 0, "processing": 0, "review": 50, "failed": 0}, providers=[])
    st = PagerState()
    _, st = plan_scan(w, c, st, at("14:19"))
    out, st = plan_scan(w, c, st, at("14:20"))
    assert [k for m in out.messages for k in m.alert_keys] == ["review_backlog"] and st.held == []
