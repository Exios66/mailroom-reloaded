"""Live-production run on mailroom-reloaded, end to end with no mocked snapshot.

Real archive ledger + real watcher.lock + the real Pager.tick and collect_snapshot.
Only the phone is fake (it records what each recipient would receive).
Timeline: healthy -> 3 docs fail -> watcher process dies -> watcher back, failures age out."""
import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

out_dir = sys.argv[1]
base = tempfile.mkdtemp()
os.environ["MAILROOM_BASE_DIR"] = base
os.environ.pop("MAILROOM_PAGER", None)

from mailroom_reloaded.obs import pager
from mailroom_reloaded.obs.pager_core import PagerConfig
from mailroom_reloaded.obs.run_context import live_run_id
from mailroom_reloaded.obs.sms import SendResult, count_segments
from mailroom_reloaded.pipeline import run_ledger
from mailroom_reloaded.storage.ledger import get_ledger

TZ = ZoneInfo("America/Chicago")
lg = get_ledger(anchor=False)
run = live_run_id()
run_ledger.open_run(lg, run, "live", environment="live", source="watch")
n = [1830]


def close(outcome):
    p = {"outcome": outcome, "duration_s": 44.0, "usage_by_role": {"sorter": {"cost_usd": 0.004}, "specialist": {"cost_usd": 0.011}}}
    if outcome == "failed":
        p.update(failure_reason="llm_error", failure_class="llm_error")
    lg.append("doc_closed", run, doc_id=f"claim_{n[0]:04d}", payload=p)
    lg.flush()
    n[0] += 1


watcher = open(os.path.join(base, "watcher.lock"), "w")  # noqa: SIM115 — held like the watcher holds it
fcntl.flock(watcher, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a running `mailroom watch` holds this


class Phone:
    name = "twilio"
    def __init__(self): self.inbox = []
    def send(self, to, body):
        self.inbox.append({"to": to, "body": body, "at": self.now})
        return SendResult(True, "twilio", count_segments(body)[1], "SM" + "0" * 32)


cfg = PagerConfig.resolve({"enabled": True, "recipients": ["+16085551234", "+14145559876"], "timezone": "America/Chicago"}, {}, environment="live")
phone = Phone()
p = pager.Pager(cfg, transport=phone)
t0 = time.time()
events = {}
for _ in range(9): close("completed")
for _ in range(2): close("parked")
for minute in range(34):
    now = t0 + minute * 60
    if minute == 3:
        for _ in range(3): close("failed")
    if minute == 5:
        watcher.close()  # watcher process exits
    if minute == 12:
        watcher = open(os.path.join(base, "watcher.lock"), "w")  # noqa: SIM115
        fcntl.flock(watcher, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(4): close("completed")
    phone.now = now
    p.tick(now)
seen, msgs = set(), []
for m in phone.inbox:
    if m["body"] not in seen:
        seen.add(m["body"])
        msgs.append({"at": datetime.fromtimestamp(m["at"], TZ).strftime("%-I:%M:%S %p"), "body": m["body"],
                     "recipients": sorted(x["to"] for x in phone.inbox if x["body"] == m["body"])})
with open(f"{out_dir}/messages.json", "w") as fh:
    json.dump({"texts_delivered": len(phone.inbox), "messages": msgs}, fh, indent=1, ensure_ascii=False)
with open(os.path.join(base, "pager_log.jsonl")) as fh:
    log = fh.read()
with open(f"{out_dir}/pager_log.jsonl", "w") as fh:
    fh.write(log)
print(f"texts_delivered={len(phone.inbox)} distinct_messages={len(msgs)} log_entries={len(log.splitlines())}")
for m in msgs:
    print(f"\n[{m['at']}] → {len(m['recipients'])} recipients\n{m['body']}")
