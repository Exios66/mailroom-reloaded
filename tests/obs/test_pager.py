"""Operator SMS pager host (ledger-backed snapshot, scheduler, CLI, start prompt).

Hermetic: a recording transport replaces Twilio, snapshots are fed directly for
the scheduler tests, and the snapshot tests seed a real ledger in a tmp base dir."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from mailroom_reloaded.obs.pager_core import PagerConfig, RunSnapshot
from mailroom_reloaded.obs.pager_core import ProviderStatus as PS
from mailroom_reloaded.obs.sms import SendResult, set_transport_override

TZ = ZoneInfo("America/Chicago")
MOBILE, GV = "+16085551234", "+14145559876"


def at(hhmm, day=15):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


class Rec:
    name = "rec"

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, to, body):
        self.sent.append((to, body))
        return SendResult(self.ok, "rec", 2, "SM123" if self.ok else "HTTP 500")


def healthy(now, **kw):
    d = {"taken_at": now, "mode": "production", "watcher_alive": True, "heartbeat_age_s": None, "uptime_s": None,
         "sha": "abc", "queues": {"inbox": 0, "processing": 0, "review": 0, "failed": 0},
         "window": {"minutes": 10, "received": 2, "archived": 2, "review": 0, "failed": 0}, "stuck": 0,
         "paused": None, "outbox_dead": None, "providers": [PS("openrouter", "up")], "runs": [], "failures": [],
         "openrouter": None, "today": None, "host": "mac", "repo": "mailroom-reloaded"}
    d.update(kw)
    return RunSnapshot(**d)


def broken(now):
    return healthy(now, providers=[PS("modal:vllm", "down", "HTTP 502", 5, 5)])


@pytest.fixture
def base(monkeypatch, tmp_path):
    import os

    for k in [k for k in os.environ if k.startswith("MAILROOM_PAGER")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MAILROOM_PAGER_TZ", "America/Chicago")
    monkeypatch.delenv("MAILROOM_ENVIRONMENT", raising=False)
    from mailroom_reloaded.storage import db
    from mailroom_reloaded.storage.ledger import reset_ledger

    # The default engine is process-wide: without this, another test's ledger leaks into snapshots.
    reset_ledger()
    monkeypatch.setattr(db, "_default_engine", None)
    yield tmp_path
    reset_ledger()
    if db._default_engine is not None:
        db._default_engine.dispose()
    db._default_engine = None


@pytest.fixture
def host(base, monkeypatch):
    from mailroom_reloaded.obs import pager

    feed = {"fn": healthy}
    monkeypatch.setattr(pager, "collect_snapshot", lambda cfg, window_minutes, now=None: feed["fn"](now or time.time()))
    rec = Rec()
    set_transport_override(rec)
    yield pager, rec, feed
    set_transport_override(None)


def make(pager, rec, **block):
    cfg = {"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago", **block}
    return pager.Pager(PagerConfig.resolve(cfg, {}, environment="live"), transport=rec)


def ticks(p, start, minutes, step=20):
    return [r for t in range(0, int(minutes * 60), step) for r in p.tick(start + t)]


# ── snapshot ─────────────────────────────────────────────────────────


@pytest.fixture
def ledger(base, monkeypatch):
    from mailroom_reloaded.storage import ledger as ledger_mod
    from mailroom_reloaded.storage.db import init_db

    lg = ledger_mod.Ledger(init_db(base / "mailroom.db"))
    monkeypatch.setattr(ledger_mod, "get_ledger", lambda **kw: lg)
    yield lg
    lg.close()


def _close(lg, run, doc, outcome, cost=0.01, dur=40.0, **extra):
    lg.append("doc_closed", run, doc_id=doc, payload={
        "outcome": outcome, "duration_s": dur, "usage_by_role": {"sorter": {"cost_usd": cost}}, **extra})


def test_snapshot_from_seeded_ledger_window(ledger, monkeypatch):
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.obs.run_context import live_run_id

    run = live_run_id()
    for i, o in enumerate(["completed", "completed", "parked", "failed"]):
        _close(ledger, run, f"d{i}", o, dur=10.0 * (i + 1), failure_reason="llm_error")
    _close(ledger, "live-19990101", "old", "completed")  # another run: never counted
    ledger.flush()
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE]}, {}, environment="live")
    snap = pager.collect_snapshot(cfg, window_minutes=10)
    w = snap.window
    assert (w["received"], w["archived"], w["review"], w["failed"]) == (4, 2, 1, 1)
    assert w["cost_usd"] == 0.04 and w["p50_latency_s"] == 25.0
    assert [(f["name"], f["error"]) for f in snap.failures] == [("d3", "llm_error")]
    assert snap.today["processed"] == 4 and snap.repo == "mailroom-reloaded"


def test_ledger_failure_snapshot_renders_alert(ledger):
    """A failed document in the ledger must page, not crash the scan (failures carry epoch floats)."""
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.obs.pager_core import PagerState, plan_scan
    from mailroom_reloaded.obs.run_context import live_run_id

    for i in range(3):
        _close(ledger, live_run_id(), f"f{i}", "failed", failure_reason="llm_error")
    ledger.flush()
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago"}, {},
                              environment="live")
    snap = pager.collect_snapshot(cfg, window_minutes=10)
    assert all(isinstance(f["at"], float) for f in snap.failures)
    out, _ = plan_scan(snap, cfg, PagerState(), snap.taken_at)
    assert any("failed" in m.text for m in out.messages)


def test_window_spans_utc_date_rollover_of_live_run_id(ledger):
    """Live run ids roll at UTC midnight (7 PM CDT); a window that spans the
    rollover still counts the documents closed under the previous day's run."""
    from datetime import UTC, timedelta

    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.obs.run_context import live_run_id

    wall = time.time()
    run = live_run_id(datetime.fromtimestamp(wall, UTC))
    for i in range(3):
        _close(ledger, run, f"f{i}", "failed", failure_reason="llm_error")
    ledger.flush()
    midnight = datetime.fromtimestamp(wall, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    now = (midnight + timedelta(days=1, minutes=1)).timestamp()  # just past the rollover
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago"}, {},
                              environment="live")
    snap = pager.collect_snapshot(cfg, window_minutes=(now - wall) / 60 + 5, now=now)
    assert (snap.window["received"], snap.window["failed"]) == (3, 3)
    assert len(snap.failures) == 3


def test_live_run_ids_cover_every_utc_date_in_range(monkeypatch):
    from mailroom_reloaded.obs import pager

    monkeypatch.delenv("MAILROOM_RUN_ID", raising=False)
    day = 86400
    t = datetime(2026, 10, 15, 23, 55, tzinfo=ZoneInfo("UTC")).timestamp()
    assert pager._live_run_ids(t, t + 600) == ["live-20261015", "live-20261016"]
    assert pager._live_run_ids(t - 2 * day, t) == ["live-20261013", "live-20261014", "live-20261015"]
    monkeypatch.setenv("MAILROOM_RUN_ID", "pinned")
    assert pager._live_run_ids(t, t + 600) == ["pinned"]


def test_eval_run_is_progress_with_total(ledger):
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.pipeline import run_ledger

    assert run_ledger.open_run(ledger, "ev1", "eval", posture_label="pipeline", total=5)
    ledger.flush()
    _close(ledger, "ev1", "a", "completed")
    _close(ledger, "ev1", "b", "failed")
    ledger.flush()
    [r] = pager.eval_runs(ledger, time.time())
    assert (r.run_id, r.label, r.total, r.done, r.finished) == ("ev1", "eval pipeline", 5, 2, False)
    assert r.metrics["failed"] == 1


def test_closed_eval_run_is_reported_finished(ledger):
    """A closed eval run is reported finished (for a day), so `eval --page` sends its finished text."""
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.pipeline import run_ledger

    assert run_ledger.open_run(ledger, "ev2", "eval", posture_label="pipeline", total=2)
    ledger.flush()
    _close(ledger, "ev2", "a", "completed")
    _close(ledger, "ev2", "b", "completed")
    run_ledger.close_run(ledger, "ev2")
    ledger.flush()
    [r] = pager.eval_runs(ledger, time.time())
    assert (r.run_id, r.done, r.finished) == ("ev2", 2, True)
    assert pager.eval_runs(ledger, time.time() + 2 * 86400) == []  # a day-old finish is not re-announced


def test_snapshot_survives_missing_ledger(base, monkeypatch):
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.storage import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "get_ledger", lambda **kw: (_ for _ in ()).throw(OSError("db gone")))
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE]}, {}, environment="live")
    snap = pager.collect_snapshot(cfg, window_minutes=10)
    assert snap.window == {} and snap.runs == [] and snap.queues["inbox"] == 0


def test_watcher_lock_free_means_down(base):
    import fcntl

    from mailroom_reloaded.obs.pager import watcher_running

    assert watcher_running(base) is False  # no lock file
    (base / "watcher.lock").write_text("123")
    assert watcher_running(base) is False  # free lock: nobody is draining
    with open(base / "watcher.lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        assert watcher_running(base) is True


# ── scheduler ────────────────────────────────────────────────────────


def test_digest_every_10_minutes_aligned(host):
    pager, rec, _ = host
    ticks(make(pager, rec), at("14:00") + 5, 61)
    assert len(rec.sent) == 6 and all(b.startswith("Mailroom LIVE · ") for _, b in rec.sent)
    assert rec.sent[0][1].splitlines()[0].endswith(" 2:10 PM CDT")


def test_failed_send_keeps_alert_pending(host):
    pager, _, feed = host
    feed["fn"] = broken
    bad = Rec(ok=False)
    p = make(pager, bad)
    p.tick(at("14:01"))
    assert len(bad.sent) == 1 and "provider_down:modal:vllm" in p.state.pending
    bad.ok = True
    for m in ("14:02", "14:03", "14:04"):
        p.tick(at(m))
    assert sum(b.startswith("Mailroom ALERT") for _, b in bad.sent) == 2 and p.state.pending == []


def test_state_survives_restart(host):
    pager, rec, feed = host
    feed["fn"] = broken
    make(pager, rec).tick(at("14:01"))
    p2 = make(pager, rec)
    p2.tick(at("14:03"))
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1
    assert p2.state.next_digest == at("14:10")


def test_watcher_down_copy_names_mailroom_watch(host):
    pager, rec, feed = host
    feed["fn"] = lambda now: healthy(now, watcher_alive=False)
    make(pager, rec).tick(at("14:01"))
    assert any("mailroom watch" in b for _, b in rec.sent)


# ── CLI (typer sub-app) ──────────────────────────────────────────────


def cli(*args):
    from mailroom_reloaded.cli import app

    return CliRunner().invoke(app, ["pager", *args])


def test_cli_pager_preview_prints_digest(host):
    r = cli("preview", "--mode", "production")
    assert r.exit_code == 0 and r.stdout.startswith("Mailroom LIVE · ") and "Ref P-" in r.stdout
    _, rec, _ = host
    assert rec.sent == []
    r = cli("preview", "--json")
    assert json.loads(r.stdout)["schema"] == "mailroom.pager/v1"


def test_cli_on_off_persists_and_running_pager_picks_up(host):
    pager, rec, feed = host
    feed["fn"] = broken
    assert cli("on", "--to", MOBILE).exit_code == 0
    p = pager.Pager(transport=rec)
    assert cli("off").exit_code == 0
    p.tick(at("14:01"))
    assert rec.sent == [] and pager.read_settings()["recipients"] == [MOBILE]
    assert cli("on").exit_code == 0
    p.tick(at("14:02"))
    assert len(rec.sent) == 1


def test_cli_number_set_add_remove_list_masked(host):
    pager, *_ = host
    r = cli("number", "set", "608-555-1234")
    assert r.exit_code == 0 and "+16085551234" in r.stdout
    cli("number", "add", GV)
    out = cli("number", "list").stdout
    assert "+1•••••1234" in out and "+1•••••9876" in out and MOBILE not in out
    assert GV in cli("number", "list", "--show").stdout
    cli("number", "remove", MOBILE)
    assert pager.read_settings()["recipients"] == [GV]


def test_cli_bad_number_exits_2(host):
    r = cli("number", "set", "12345")
    assert r.exit_code == 2 and "not a valid number — use E.164, e.g. +16085551234" in r.stderr


def test_cli_on_without_number_non_tty_exits_2(host, monkeypatch):
    pager, *_ = host
    monkeypatch.setattr(pager, "_tty", lambda: False)
    r = cli("on")
    assert r.exit_code == 2 and "no number saved — use: pager on --to +1…" in r.stderr


def test_cli_test_to_adhoc_number_does_not_save(host):
    pager, rec, _ = host
    assert cli("test", "--to", "(414) 555-9876").exit_code == 0
    assert rec.sent[0][0] == GV and rec.sent[0][1].startswith("✅ MAILROOM PAGER TEST · ")
    assert pager.read_settings().get("recipients") in (None, [])
    set_transport_override(Rec(ok=False))
    assert cli("test", "--to", GV).exit_code == 1


def test_cli_mute_writes_state(host, base):
    assert cli("mute", "2h").exit_code == 0
    assert json.loads((base / "pager_state.json").read_text())["muted_until"] > time.time() + 7000


# ── start prompt and hooks ───────────────────────────────────────────


def _answers(*xs):
    it = iter(xs)

    def fn(prompt):
        v = next(it)
        if isinstance(v, BaseException):
            raise v
        return v
    return fn


def test_prompt_enable_with_number_and_test(host):
    pager, rec, _ = host
    cfg = pager.prompt_on_start(pager.load_pager_config({}), flag=None,
                                input_fn=_answers("y", "608 555 1234", ""), isatty=True)
    assert cfg.enabled and cfg.recipients == (MOBILE,)
    assert pager.read_settings()["updated_by"] == "prompt"
    assert len(rec.sent) == 1 and "PAGER TEST" in rec.sent[0][1]


def test_prompt_default_keeps_off_on_timeout(host):
    pager, rec, _ = host
    cfg = pager.prompt_on_start(pager.load_pager_config({}), flag=None, input_fn=_answers(TimeoutError()), isatty=True)
    assert not cfg.enabled and rec.sent == []


def test_prompt_skipped_non_tty_logs_hint(host, capsys):
    pager, *_ = host
    cfg = pager.prompt_on_start(pager.load_pager_config({}), flag=None, input_fn=_answers(), isatty=False)
    assert not cfg.enabled and "pager_prompt_skipped" in capsys.readouterr().out


def test_prompt_skipped_in_research_mode(host):
    pager, *_ = host
    research = pager.load_pager_config({"MAILROOM_ENVIRONMENT": "eval"})
    assert pager.prompt_on_start(research, flag=None, input_fn=_answers(), isatty=True) is research


def test_watch_no_pager_flag_skips_prompt(host, monkeypatch):
    pager, *_ = host
    from mailroom_reloaded import watcher

    asked = []
    monkeypatch.setattr(pager, "_ask", lambda *a, **k: asked.append(a) or "y")
    monkeypatch.setattr(watcher.Watcher, "run_forever", lambda self: None)
    from mailroom_reloaded.cli import app

    r = CliRunner().invoke(app, ["watch", "--no-pager"])
    assert r.exit_code == 0 and asked == [] and pager.read_settings()["enabled"] is False


def test_lifespan_hosts_an_idle_pager_so_pager_on_takes_effect(host, monkeypatch):
    pager, *_ = host
    from fastapi.testclient import TestClient

    from mailroom_reloaded.api.app import create_app

    started = []
    monkeypatch.setattr(pager.Pager, "start_background",
                        lambda self, idle_ok=False: started.append((self._own_cfg, idle_ok)))
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    with TestClient(create_app()):
        pass
    assert started == [(True, True)]  # off at start, still polling settings
    monkeypatch.setenv("MAILROOM_PAGER_EMBED", "0")
    with TestClient(create_app()):
        pass
    assert started == [(True, True)]


def test_watch_hosts_an_idle_pager_so_pager_on_takes_effect(host, monkeypatch):
    pager, *_ = host
    from mailroom_reloaded import watcher

    started = []
    monkeypatch.setattr(pager.Pager, "start_background",
                        lambda self, idle_ok=False: started.append((self._own_cfg, idle_ok)))
    monkeypatch.setattr(watcher.Watcher, "run_forever", lambda self: None)
    from mailroom_reloaded.cli import app

    r = CliRunner().invoke(app, ["watch", "--no-pager"])
    assert r.exit_code == 0 and started == [(True, True)]


def test_openrouter_info_refuses_plain_http_for_remote_hosts(host, monkeypatch):
    pager, *_ = host
    cfg = pager.load_pager_config()
    monkeypatch.setattr(pager, "get_settings", lambda: type("S", (), {"openrouter_api_key": "sk-real"})())
    pager._OR_CACHE.clear()
    sent = []

    class C:
        def get(self, url, **kw):
            sent.append(url)
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": {}}})()

    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://proxy.example.com/v1")
    assert pager.openrouter_info(cfg, client=C()) == {"error": "insecure base URL"}
    assert sent == []
    pager._OR_CACHE.clear()
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://localhost:4000/v1")
    assert "error" not in pager.openrouter_info(cfg, client=C())
    assert sent == ["http://localhost:4000/v1/key"]


def test_preview_mode_research_collects_research_stats(host, monkeypatch):
    pager, *_ = host
    seen = []
    real = pager.collect_snapshot
    monkeypatch.setattr(pager, "collect_snapshot", lambda cfg, **kw: seen.append(cfg.run_mode) or real(cfg, **kw))
    r = cli("preview", "--mode", "research")
    assert r.exit_code == 0 and seen == ["research"]


# ── final-review fixes ───────────────────────────────────────────────


def test_explicit_config_pager_honors_cli_off_and_number_changes(host):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)  # serve/watch/eval --page pass an explicit cfg
    p.tick(at("14:01"))
    assert len(rec.sent) == 1
    assert pager.main(["off"]) == 0
    for m in range(2, 50):
        p.tick(at("14:00") + m * 60)
    assert len(rec.sent) == 1
    assert pager.main(["number", "set", GV]) == 0 and pager.main(["on"]) == 0
    p.tick(at("14:51"))
    assert rec.sent[-1][0] == GV


def test_state_save_failure_does_not_resend_alerts(host, monkeypatch):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)

    def full(_):
        raise OSError("No space left on device")
    monkeypatch.setattr(pager, "save_state", full)
    for m in range(1, 7):
        p.tick(at("14:00") + m * 60)
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1


def test_cli_mute_reaches_running_pager_without_clobbering_its_state(host, base):
    pager, rec, feed = host
    feed["fn"] = broken
    p = make(pager, rec)
    p.tick(at("14:01"))
    key = next(iter(p.state.open))
    sent_at = p.state.open[key]["last_sent"]
    (base / "pager_state.json").write_text("{}")  # a CLI read-modify-write from a stale copy
    assert pager.main(["mute", "2h"]) == 0
    p.tick(at("14:02"))
    assert p.state.muted_until and p.state.open[key]["last_sent"] == sent_at
    assert sum(b.startswith("Mailroom ALERT") for _, b in rec.sent) == 1


def test_flush_sends_run_finished_before_exit(host):
    from mailroom_reloaded.obs.pager_core import RunProgress

    pager, rec, feed = host
    run = RunProgress("r1", "mailroom eval", 3, 0, at("14:00"), at("14:00"), False, {})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[run])
    p = make(pager, rec, mode="research")
    p.tick(at("14:00"))
    done = RunProgress("r1", "mailroom eval", 3, 3, at("14:00"), at("14:00") + 20, True, {"failed": 0})
    feed["fn"] = lambda now: healthy(now, mode="research", runs=[done])
    p.flush(at("14:00") + 21)  # well inside the 60 s scan interval
    assert [b.splitlines()[1][:14] for _, b in rec.sent] == ["▶ Run started:", "✅ Run finished"]


def test_research_background_pager_flushes_at_exit(host, monkeypatch):
    pager, rec, _ = host
    registered = []
    monkeypatch.setattr("atexit.register", lambda fn, *a, **k: registered.append(fn))
    p = make(pager, rec, mode="research")
    thread = None
    try:
        thread = p.start_background()
        assert thread is not None
        assert p.flush in registered
    finally:
        p.stop()
        if thread is not None:
            thread.join(timeout=5)


def test_digest_snapshot_only_taken_when_due(host):
    pager, rec, feed = host
    calls = []
    feed["fn"] = lambda now: calls.append(now) or healthy(now)
    p = make(pager, rec, digest={"production_minutes": 0})
    ticks(p, at("14:00") + 5, 3, step=15)  # 12 host ticks, 3 scans
    assert len(calls) == 3
    calls.clear()
    p.release()
    q = make(pager, rec, quiet_hours={"window": "22:00-07:00"})
    ticks(q, at("23:00") + 5, 3, step=15)
    assert len(calls) == 3


def test_research_eval_does_not_page_watcher_down(base, monkeypatch):
    from mailroom_reloaded.obs import pager
    from mailroom_reloaded.obs.pager_core import PagerState, evaluate_rules, plan_scan

    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE], "mode": "research"}, {}, environment="live")
    snap = pager.collect_snapshot(cfg, window_minutes=10)
    assert snap.watcher_alive is False  # no `mailroom watch` running
    assert "watcher_down" not in [a.key for a in evaluate_rules(snap, cfg, {})]
    out, _ = plan_scan(snap, cfg, PagerState(), snap.taken_at)
    assert not [m for m in out.messages if m.kind == "alert"]  # a run_started text is fine; an alert is not


def test_cli_accepts_gateway_address_with_email_gateway_transport(host, monkeypatch):
    pager, *_ = host
    monkeypatch.setenv("MAILROOM_PAGER_TRANSPORT", "email_gateway")
    assert pager.main(["on", "--to", "6085551234@vtext.com"]) == 0
    assert pager.load_pager_config().recipients == ("6085551234@vtext.com",)


def test_preview_digest_reflects_active_failures(host):
    _, _, feed = host
    feed["fn"] = broken
    r = cli("preview")
    assert r.exit_code == 0 and "All clear" not in r.output


def test_partial_delivery_retries_only_failed_recipients(host):
    pager, _, feed = host

    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    feed["fn"] = broken
    t = Flaky()
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                                        {}, environment="live"), transport=t)
    p.tick(at("14:01"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]
    assert all(not e.get("last_sent") for e in p.state.open.values())  # not acked: GV still owed
    assert pager._read_log()[0]["delivery_partial"] is True
    t.down, t.sent = set(), []
    p.tick(at("14:01") + 90)
    assert [to for to, _ in t.sent] == [GV]  # MOBILE is not texted twice
    assert all(e.get("last_sent") for e in p.state.open.values())
    assert p.state.delivered == {}
    p.tick(at("14:01") + 180)
    assert [to for to, _ in t.sent] == [GV]  # acked: no further retry


def test_idle_ok_pager_keeps_polling_when_another_sender_holds_the_lock(host):
    pager, rec, _ = host
    first, second = make(pager, rec), make(pager, rec)
    assert first.acquire()
    thread = None
    try:
        thread = second.start_background(idle_ok=True)
        assert thread is not None and second._lock is None  # passive, but still polling
        assert make(pager, rec).start_background() is None  # batch hosts still stand down
        first.release()
        second.tick(at("14:00") + 5)
        assert second._lock is not None  # took over once the owner left
    finally:
        second.stop()
        if thread is not None:
            thread.join(timeout=5)
        second.release()
        first.release()


def test_credentialless_pager_does_not_take_the_send_lock(host, monkeypatch):
    pager, rec, feed = host
    set_transport_override(None)  # real credential checks, not the test override
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_MESSAGING_SERVICE_SID"):
        monkeypatch.delenv(k, raising=False)
    feed["fn"] = broken
    cfg = {"enabled": True, "recipients": [MOBILE], "timezone": "America/Chicago", "transport": "twilio"}
    blind = pager.Pager(PagerConfig.resolve(cfg, {}, environment="live"))  # embedded API pager: no creds
    sender = pager.Pager(PagerConfig.resolve(cfg, {}, environment="live"), transport=rec)
    thread = blind.start_background(idle_ok=True)  # starts first
    try:
        assert thread is not None and blind._lock is None  # idle, polling, not holding the lock
        assert blind.tick(at("14:01")) == [] and blind._lock is None
        assert sender.tick(at("14:01")) and rec.sent  # the pager that can deliver wins the lock
        assert sender._lock is not None
        sender.release()
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")  # creds appear later: it competes normally
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_FROM_NUMBER", "+16085550000")
        blind.tick(at("14:02"))
        assert blind._lock is not None
    finally:
        blind.stop()
        if thread is not None:
            thread.join(timeout=5)
        blind.release()
        sender.release()


def test_new_digest_is_not_limited_by_an_earlier_partial_digest(host):
    pager, _, _ = host

    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    t = Flaky()
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                                        {}, environment="live"), transport=t)
    p.tick(at("14:10") + 5)  # first digest: GV fails
    t.down, t.sent = set(), []
    p.tick(at("14:20") + 5)  # next digest is new text: every recipient gets it
    assert [to for to, _ in t.sent] == [MOBILE, GV]


def test_cap_limits_recipients_texted_and_the_rest_stay_pending(host):
    pager, rec, feed = host
    feed["fn"] = lambda now: healthy(now, queues={"inbox": 0, "processing": 0, "review": 50, "failed": 0})
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago",
                                         "alerts": {"max_texts_per_hour": 1}}, {}, environment="live"), transport=rec)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    from mailroom_reloaded.obs.pager_core import Message
    p._deliver(Message("alert", "r1", "x", {}, ("review_backlog",)), at("14:01"))
    assert [to for to, _ in rec.sent] == [MOBILE]  # cap of 1: only one recipient this hour
    assert len(p.state.sent_log) == 1 and not p.state.open["review_backlog"]["last_sent"]
    assert "review_backlog" in p.state.pending


def _warn_pager(pager, rec, cap):
    p = pager.Pager(PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago",
                                         "alerts": {"max_texts_per_hour": cap}}, {}, environment="live"), transport=rec)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    return p


def test_delivery_record_outlives_an_hour_while_the_alert_is_unacked(host):
    pager, rec, _ = host
    from mailroom_reloaded.obs.pager_core import Message
    p = _warn_pager(pager, rec, 1)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))
    p._deliver(msg, at("15:30"))  # allowance is back: the other recipient, not the first again
    assert [to for to, _ in rec.sent] == [MOBILE, GV]


def test_recurring_alert_does_not_inherit_the_old_incident_delivery(host):
    pager, rec, _ = host
    from mailroom_reloaded.obs.pager_core import Message
    p = _warn_pager(pager, rec, 1)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # MOBILE only
    p.state.open["review_backlog"]["since"] = at("16:00")  # cleared quietly, then fired again
    p._deliver(msg, at("16:01"))
    assert [to for to, _ in rec.sent] == [MOBILE, MOBILE]


def _flaky_pager(pager):
    class Flaky(Rec):
        def __init__(self):
            super().__init__()
            self.down = {GV}

        def send(self, to, body):
            self.ok = to not in self.down
            return super().send(to, body)

    t = Flaky()
    cfg = PagerConfig.resolve({"enabled": True, "recipients": [MOBILE, GV], "timezone": "America/Chicago"},
                              {}, environment="live")
    p = pager.Pager(cfg, transport=t)
    p.state.open["review_backlog"] = {"since": at("14:00"), "fail": 5, "clear": 0, "last_sent": None,
                                      "severity": "warning", "title": "Review backlog", "detail": "", "next_step": ""}
    return p, t


def test_alert_that_recurs_within_the_ttl_goes_to_everyone_again(host):
    pager, _, _ = host
    from mailroom_reloaded.obs.pager_core import Message
    p, t = _flaky_pager(pager)
    msg = Message("alert", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # MOBILE ok, GV fails: partial
    assert [to for to, _ in t.sent] == [MOBILE, GV]
    p.state.open["review_backlog"]["since"] = at("14:20")  # cleared, then a new incident
    t.down, t.sent = set(), []
    p._deliver(msg, at("14:21"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]


def test_recovery_that_recurs_does_not_reuse_a_partial_recovery_record(host):
    pager, _, _ = host
    from mailroom_reloaded.obs.pager_core import Message
    p, t = _flaky_pager(pager)
    e = p.state.open.pop("review_backlog")
    p.state.recovered["review_backlog"] = {**e, "open_s": 60.0}
    msg = Message("resolved", "r1", "x", {}, ("review_backlog",))
    p._deliver(msg, at("14:01"))  # partial
    p.state.recovered["review_backlog"]["since"] = at("14:20")  # the alert fired and recovered again
    t.down, t.sent = set(), []
    p._deliver(msg, at("14:21"))
    assert [to for to, _ in t.sent] == [MOBILE, GV]
