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
    return tmp_path


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
    assert snap.failures == [{"name": "d3", "error": "llm_error", "at": snap.failures[0]["at"]}]
    assert snap.today["processed"] == 4 and snap.repo == "mailroom-reloaded"


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


def test_lifespan_starts_pager_only_when_enabled(host, monkeypatch):
    pager, *_ = host
    from fastapi.testclient import TestClient

    from mailroom_reloaded.api.app import create_app

    started = []
    monkeypatch.setattr(pager.Pager, "start_background", lambda self: started.append(self.cfg.active))
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    with TestClient(create_app()):
        pass
    assert started == []
    pager.write_settings(enabled=True, recipients=[MOBILE], by="test")
    with TestClient(create_app()):
        pass
    assert started == [True]
    monkeypatch.setenv("MAILROOM_PAGER_EMBED", "0")
    with TestClient(create_app()):
        pass
    assert started == [True]
