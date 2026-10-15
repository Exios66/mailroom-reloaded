"""Seed a real archive ledger (live run) with terminal documents: 11 archived, 2 parked, 3 failed."""
from mailroom_reloaded.obs.run_context import live_run_id
from mailroom_reloaded.pipeline import run_ledger
from mailroom_reloaded.storage.ledger import get_ledger

lg = get_ledger(anchor=False)
run = live_run_id()
run_ledger.open_run(lg, run, "live", environment="live", source="watch")
outs = ["completed"] * 11 + ["parked"] * 2 + ["failed"] * 3
for i, o in enumerate(outs):
    p = {"outcome": o, "duration_s": 40.0 + i, "usage_by_role": {"sorter": {"cost_usd": 0.004}, "specialist": {"cost_usd": 0.011}}}
    if o == "failed":
        p.update(failure_reason="llm_error", failure_class="llm_error")
    lg.append("doc_closed", run, doc_id=f"claim_{1830 + i:04d}", payload=p)
lg.flush()
print("seeded", len(outs), "doc_closed entries in", run)
