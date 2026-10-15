# Operator SMS pager — 2026-10-15

What an operator's phone receives during a live production run of
`mailroom-reloaded`. Nothing in the pager is mocked. The run uses a real
archive ledger (`doc_closed` entries under the live run id), a real
`watcher.lock` held the way `mailroom watch` holds it, and the real
`obs.pager.collect_snapshot`, `Pager.tick` scan/digest scheduling, rendering,
per-recipient delivery and `pager_log.jsonl`. Only the phone is a recording
transport standing in for Twilio.

## Scenario (two recipients, one host tick per minute)

| Time (CDT) | Event | Text |
|---|---|---|
| 6:54 | 11 documents closed (9 archived, 2 to review) | none (healthy, so scans stay silent) |
| 6:57 → 6:58 | 3 documents fail (`llm_error`) | 🟠 ALERT |
| 6:59 | watcher process exits (lock released) | 🔴 ALERT (critical, with the failures as context) |
| 7:00 | 10-minute digest | digest that opens with the open problem |
| 7:06, 7:07 | failures age out; watcher back | ✅ RESOLVED, one text per recovery |
| 7:10, 7:20 | healthy | 🟢 digests |

The run sent 7 distinct texts and made 14 deliveries (7 × 2 recipients). The
7:00 PM CDT digest falls on the live run id's UTC-midnight rollover. It still
counts all 14 documents because the pager reads every live run id in the
window (`c8996aa`, found by this run).

| File | What it is |
|---|---|
| `01-alerts-failures-watcher-down.png` | the two alerts |
| `02-digest-open-alert.png` | digest during the incident |
| `03-resolved-all-clear.png` | recovery texts and the next all-clear digest |
| `04-full-incident-thread.png` | the whole thread as one phone shows it |
| `messages.json` | what each phone received (numbers masked) |
| `pager_log.jsonl` | machine-readable `mailroom.pager/v1` record of every send |
| `cli-transcript.txt` | every `mailroom pager` verb against a seeded ledger, with exit codes |
| `simulate_reloaded_run.py`, `seed_ledger.py`, `render_phone.py`, `shoot.sh` | scripts that reproduce the above |

## Reproduce

```bash
uv run python docs/evidence/2026-10-15-operator-sms-pager/simulate_reloaded_run.py /tmp/out
python docs/evidence/2026-10-15-operator-sms-pager/render_phone.py /tmp/out/messages.json /tmp/out/thread.html "Mailroom" 0,1,2,3,4,5,6
```

Times follow the wall clock at run time, so a rerun gives the same sequence of
texts at different clock times.
