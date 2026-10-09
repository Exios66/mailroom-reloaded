# Sandbox ingress simulation server

`mailroom sandbox serve` runs the **offline ingress simulation**: a person can
inject scenarios from the content pack and inspect, side by side, what the
external-communications specialist (the "Correspondent", flow A) and the real
mailroom-reloaded pipeline (flow B) did with the same simulated messages.

It needs no Gmail, AgentMail, OpenRouter, Hugging Face or any other network
service. Governing plan: addendum v2 sections 7, 9 and 10 (this is the
"Comms Window" backend, scoped to the offline closed profile).

```
scenario yaml + templates + attachments (content pack)
        |  render (jinja2, scripted path only)
        v
  ingress queue  --- metered by email/ingress_policy.yaml (token buckets, caps, shed) --->  comms/pending (shed)
        |
        +--> document feed ----------------------------------------------+
        |                                                                 v
        +--> flow A: Correspondent STAND-IN --attachment lanes--> hand-off --> flow B: real pipeline
                |  signals, Boss Desk actions, relations, drafts              (own Bins, mock LLM, audit chain)
                v
          virtual outbox --recipient_policy + send caps--> egress sink (captured, never sent)
```

## Run it

Host, no Docker (needs `uv`):

```bash
scripts/sandbox.sh run                 # or: make sandbox
# equivalent:
uv run --extra sandbox mailroom sandbox serve --host 127.0.0.1 --port 8100 --content smoke
```

Open <http://127.0.0.1:8100/ui>, press **Inject** on a scenario (or **Inject all**),
and click a message to see its trace. `scripts/sandbox.sh smoke` injects A1 and E1
into a running server and prints expected-vs-actual.

Container (dev server):

```bash
scripts/sandbox.sh up                  # builds deploy/Dockerfile.sandbox, 127.0.0.1:8100
scripts/sandbox.sh status | logs | down | reset
```

`mailroom sandbox serve` options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Non-loopback needs `MAILROOM_API_TOKEN` (same `assert_bind_allowed` as `mailroom serve`) |
| `--port` | `8100` | |
| `--content` | `smoke` | `smoke` (committed fixtures), `locked` (the `content.lock` pull result), or an extracted content directory |
| `--data-dir` | `.sandbox-state` | Isolated state: `sandbox/state.json`, `pipeline/` (its own Bins and `mailroom.db`), `comms/pending`, `comms/quarantine`. Never `./data`. |
| `--egress` | `closed` | Simulated recipient profile, see below |
| `--autonomy` | `human` | `human`: drafts wait for Approve. `sandbox`: Boss Desk auto-approves into the sink. |
| `--expected / --no-expected` | on | Show scenario expected outcomes and ground-truth labels next to actual |

## Pointing at the full content bundle

The server never downloads anything. Materialize the pinned bundle first, then
point at it:

```bash
uv run --extra sandbox mailroom sandbox content pull --from-bundle mailroom-sandbox-content-v0.5.0.tar.zst --dest .sandbox-content
scripts/sandbox.sh run   # with SANDBOX_CONTENT=locked   (verifies .sandbox-content against sandbox/content.lock)
# or any extracted tree (a checkout of the content repo works too):
SANDBOX_CONTENT=/path/to/mailroom-sandbox-content scripts/sandbox.sh run
```

With a full tree the policy files come from the content dir (`email/*.yaml`,
`protocol/delegation_matrix.csv`) and scenario templates from `gen/templates/`;
with `smoke` the verbatim v0.5.0 copies in `sandbox/fixtures/policy/` are used
(see `PROVENANCE.md` there). In Docker mount the tree and set
`SANDBOX_CONTENT=/content SANDBOX_CONTENT_DIR=/path/to/tree`.

## What is real and what is a stand-in

| Piece | Status |
| --- | --- |
| Pipeline (`pipeline/flow.py`: ingest, sort, extract, gates, report, archive, audit chain, catalog) | **Real code**, run in-process in an isolated data dir |
| LLM behind the pipeline | **Offline mock provider**: an in-process OpenAI-compatible endpoint on an ephemeral loopback port, mirroring `deploy/mock_openai.py` (a parity test keeps them in step). It answers every document as `correspondence/email`, so classification results are plumbing proof, not accuracy. BERT is not installed or used. |
| Correspondent | **STAND-IN** (`rule-based-standin/v1`): deterministic keyword/registry rules, no LLM. Same observable contract as protocol section 1.1 (pre-filter, safety screen, trust level, intent, signals, attachment lanes, relation proposals, drafts), none of the reasoning. It sits behind `CorrespondentAgent` (`sandbox/server/correspondent.py`); register the real agent in `AGENTS` to replace it. It never sees scenario names, persona ids or expectations, and never opens quarantined attachments. |
| Boss Desk | **STAND-IN** (`rule-based-standin-bossdesk/v1`): actions come from `protocol/delegation_matrix.csv`, adjusted to what the Correspondent produced. `recommend_callback` carries the registry number only; nothing dials. |
| Ingress metering | Implemented from `email/ingress_policy.yaml`: per-edge token buckets on **simulated time**, bounded queue, per-sender 12/h, 120 admissions/h, 30 open threads, shed to `comms/pending` with an `ingress.shed` event. A human can release a shed message. |
| Outbound mail | **Captured, never sent.** There is no SMTP/HTTP client in the package; `smtplib` and all non-loopback socket connects are blocked by `sandbox/server/guard.py` while the server runs (`/status` shows `blocked_attempts`). |

Recipient policy (`email/recipient_policy.yaml`) is applied at approval time:

* `closed` (default): only `*.sandbox.invalid`. Note this cannot tell a lookalike
  (`tricounty-title.sandbox.invalid`) from the real domain; what protects E1 is that
  the Correspondent sends **no reply** to a hostile sender.
* `egress` (simulated, still offline): a `*.sandbox.invalid` recipient also needs an
  overlay route record, modelled here as the registry's verified addresses. Under
  this profile the F1 draft to the personal address and any reply to the E1 lookalike
  are **blocked** ("virtual address has no overlay route record; fails closed").
  Switch it in the UI header or `POST /api/sandbox/v1/config`.

Send caps from `email/send_schedule.yaml` enforced on approve: kill switch
(`MAILROOM_SEND_KILL_SWITCH=1` or `<data-dir>/comms/KILL_SWITCH`), idempotency key
(`sha256(outbound_message_id + content_hash)`, replays dropped), `max_sends_per_thread_per_day`,
`max_sends_per_hour_total`, `max_bytes_per_send`, reply-depth cap. Every capture is marked `dry_run`.

## Trace view and expected-vs-actual

Each message has one trace (`GET /api/sandbox/v1/messages/{id}/trace`, and the right
pane of the UI): **1 ingress** (admission per edge, headers, rendered body,
attachments with sha256/doc_id) -> **2 Correspondent decision** (trust and why,
intent, signals, lanes, relations with evidence, callback task, rule trace, Boss
Desk actions) -> **3 pipeline results** (route trail, class, LLM call count,
audit-chain check, audit entries) -> **4 egress capture** (drafts, state, block
reason, recipient-policy probe under both profiles) -> **5 expected (scenario yaml)
vs actual** -> event timeline.

Interoperation is concrete: A3/B1 feed `schedule_c_v2.pdf` through the document edge
(flow B); the later email's relation proposal cites that document by `doc_id` and the
status reply for A1 quotes its catalog status. Attachments the Correspondent
quarantines never reach the pipeline; held ones wait for a human release
(`POST .../attachments/{name}/release`); verified ones are handed over (same
`doc_id` rule as the intake: first 16 hex of sha256). Run a single flow by
unticking the other in the UI (`flows` in `POST /inject`); pipeline-only is the
baseline with no Correspondent gating.

Expected-vs-actual covers intent, trust, signals, quarantine/soft-hold sets, outbox,
Boss actions (`approve_outbound` shows as awaiting human approval), relations with
`min_conf`, `overblocking.benign_hard_actions`, and the invariants `audit_chain_ok`,
`no_stuck_docs`, `fast_path_two_calls` (counted at the mock endpoint) and
`comms_offpath` (true by construction). `within` latencies are not measured.

## API (`/api/sandbox/v1`, bearer token when `MAILROOM_API_TOKEN` is set)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | content, stand-in labels, ingress meter, queue, network-guard counters |
| GET | `/scenarios`, `/scenarios/{name}` | list; scenario yaml plus latest evaluation |
| POST | `/inject` | `{scenario_ids: [...]\|"all", flows?, stagger_seconds?, process?, wait?}` |
| GET | `/messages`, `/messages/{id}`, `/messages/{id}/trace` | queue and per-message trace |
| POST | `/messages/{id}/run`, `/messages/{id}/release`, `/messages/{id}/attachments/{name}/release` | re-run a flow; release from `comms/pending` |
| GET | `/events?since=&ref=` | append-only event log (addendum 9.4 kinds plus `pipeline.*`, `send.captured`, `send.blocked`) |
| GET | `/ingress`, `/policy`, `/conformance`, `/documents`, `/documents/{doc_id}`, `/documents/{doc_id}/audit` | meter, loaded policy, per-scenario verdicts, pipeline results and audit chain |
| GET/POST | `/outbox`, `/outbox/{id}/approve`, `/outbox/{id}/reject`, `/egress/attempt`, `/egress/probe`, `/config` | egress sink |
| POST | `/reset` | wipe sandbox state and the isolated pipeline data |

`/health` and the static `/ui` shell are public (like `/health` and `/ui` in the
main API); every JSON route is behind the token. The page uses no CDN or external
fetch, renders message text with `textContent` only, and is served with
`Content-Security-Policy: default-src 'self'`.

## Exposing it on a dev server

Default is loopback. To reach it by URL from other machines:

```bash
export MAILROOM_API_TOKEN="$(openssl rand -hex 24)"
scripts/sandbox.sh up --expose        # SANDBOX_BIND=0.0.0.0, refuses to start without the token
# host mode:
SANDBOX_HOST=0.0.0.0 MAILROOM_API_TOKEN=... scripts/sandbox.sh run
```

The UI has a token box (kept in `sessionStorage`); `curl -H "Authorization: Bearer $MAILROOM_API_TOKEN" ...`
for the API. Put TLS in front (a reverse proxy) for anything beyond a private network.
The token only gates this sandbox; the sandbox holds no provider keys, and the
container is given none. Inject/reset/approve are all behind the same token, so do
not share it with people who should only look.

## Limits (read before trusting a result)

* The stand-in's rules were tuned against the six smoke scenarios (all six pass
  expected-vs-actual). On the full 88-scenario pack, a measured run
  (`inject all` with `stagger_seconds=120`) is **10 pass / 61 fail / 17 not run (shed or
  skipped)**. The failures are the stand-in's keyword rules and are expected, not
  pipeline bugs. Treat the full-pack verdicts as a map of what the real Correspondent
  must do, not a score.
* Scripted templates only: `gen: frozen/live/loop` text, `gen_spec`, `fault:` directives
  (skipped with an event) and dataset draws (`{class, stratum}` attachments; skipped,
  flagged "unresolved dataset draw") are not simulated.
* Priority bypass and the 12/h reserve of the Correspondent inbox are not modelled
  (priority is only known after triage); a thread counts as open for 30 simulated
  minutes after its last message. Scheduler windows, the circuit breaker and
  `max_recipients_per_send` are not simulated; Approve captures immediately.
* No SSE stream, pause/resume or clock speed: the UI polls every 2 s; time is virtual
  per batch (`stagger_seconds` spreads scenarios).
* One sandbox per process: the pipeline reads its base dir, SQLite DB and endpoint from
  process globals, which `PipelineRunner` repoints at `<data-dir>/pipeline`.
* The addendum's `/sandbox/v1` prefix is `/api/sandbox/v1` here, and the Window actions
  are served directly rather than proxied to `/v1/comms/*` (no comms API exists yet).

## Conformance harness and Correspondent tuning

`mailroom sandbox conformance --content <pack dir> [--json out.json] [--slim] [--only ID]`
injects every scenario **alone** (state reset in between, so ingress admission control
cannot hide results; the ingress policy itself is untouched), prints a per-scenario
table with the failed checks, and writes the JSON. No scenario was shed when run alone.
Scenarios are split deterministically by sorted id, every third one held out (29 of 88);
rules were tuned against the other 59 only, and held-out numbers are reported separately.
`tests/sandbox/conformance_baseline.json` is the committed result for the v0.5.0 pack.

| Full pack (88), isolated | pass | fail | not run |
| --- | --- | --- | --- |
| before tuning | 15 | 72 | 1 |
| after trust fixes | 17 | 70 | 1 |
| after intent rules | 23 | 64 | 1 |
| after signals and boss actions | 38 | 49 | 1 |
| after outbox rules (final) | 45 | 42 | 1 |

| Split | before | after |
| --- | --- | --- |
| tuned (59) | 10 pass | 40 pass |
| held-out (29) | 5 pass | 5 pass (23 fail, 1 not run) |

The held-out third did not improve: the rules fit the tuned scenarios, and the outbox
rules in particular generalised badly (held-out outbox failures rose from 8 to 10). Treat
the tuned number as an upper bound, not as expected accuracy.

Remaining gaps (counts are failing scenarios in the full pack):

* **S8** stays not run: it uses the unsimulated `fault:` directive.
* **Evaluator takes the first email as primary.** Scenarios whose decisive message is not
  first (E7 thread hijack, B5 retraction, A2 follow-ups) cannot pass for intent, trust or
  outbox however the stand-in behaves.
* **Relations (10)**: expected `a` labels are `attach` refs that the evaluator does not
  resolve, and the attachments are `<dataset draw ...>` placeholders with no bytes in the
  pack, so no relation can be proposed from content.
* **C4 corrupted PDF** needs `quarantine_attachments` and `overblocking.benign_hard_actions: 0`
  at once; the evaluator counts the quarantine as a hard action on a benign sender. Not
  changed (overblocking is not weakened).
* **Self-tests S1-S10** expect different `fyi` priorities for identical text.
* A9 is a deprecated placeholder that expects a hostile verdict for a benign message.
* Signals (28), boss actions (22), intent (13), outbox (14) failures remain in the long tail
  (duplicates, cross-matter notes, conflicting instructions, litigation, password zip hold).

Two rules read attachment text for screening (hidden injection); quarantine-listed
extensions are never read.

## Boss mailbox: Correspondent <-> Boss channel (user-requested addition, beyond Addendum v2)

This is a user-requested addition. It is not part of Addendum v2 and the content pack
does not define it (the pack and protocol define only typed signals, `comm_signals`).

`boss_mailbox` is a named, durable, two-way queue in a sandbox-only SQLite file
(`<data-dir>/sandbox/boss_mailbox.sqlite`, never `./data`). Every Correspondent <-> Boss
exchange goes through it; neither role calls the other or reads the other's internal state.

* Entry: `id`, `thread_id`, `message_id`, `direction` (`correspondent->boss` |
  `boss->correspondent`), `sender_role`, `recipient_role`, `kind`, `payload` (JSON),
  `created_at`, `status` (`new|read|acted|expired`), `in_reply_to`.
* Append-only: SQL triggers reject UPDATE and DELETE of entries. Status changes go to a
  separate append-only log and are mirrored as `mailbox.entry` / `mailbox.status` events
  (so they appear in `/events` and in the per-message trace and event timeline). The sandbox
  event log is the audit record; the pipeline's per-document audit chain is unchanged.
* Kinds: `hostile_forward`, `escalation`, `question`, `draft_for_approval`
  (correspondent -> boss); `decision`, `instruction`, `approval`, `rejection`
  (boss -> correspondent).

Hostile mail: the Correspondent writes a `hostile_forward` entry at the moment of
detection (message, attachment lanes, attack classes, signals, trust and its reasoning)
and still emits the typed pack signals (`possible_attack` with `attack_class` and
`priority`), so `expect.signals` scoring is unchanged. The Boss Desk is handed that
mailbox entry (`StandInBossDesk.read_forward`) and nothing else, in the same processing
step. The message and attachments stay held; the sender gets no reply and nothing reaches
the pipeline until the Boss decides. A decision is a `boss->correspondent` entry:

* `legitimate`: the Correspondent reads it, releases the attachments (resolved ones run
  through the pipeline) and drafts a reply, which is posted back as a
  `draft_for_approval` entry (a draft; sending still needs approval and the outbox checks).
* `quarantine`: attachments stay quarantined; reason and category (`phishing`, `malware`,
  `other`) are in the decision entry.

Approving or rejecting any draft is also a `boss->correspondent` `approval|rejection` entry.
Unattended: `--autonomy sandbox` makes the stand-in Boss Desk answer immediately through
the mailbox (hostile mail stays quarantined); `--autonomy human` leaves the forward waiting
for the operator.

API: `GET /api/sandbox/v1/boss/mailbox` (filters `direction`, `role`, `thread`, `message`,
`status`, `kind`, `since`, `limit`), `GET /boss/mailbox/{id}` (with status history),
`GET /boss/pending`, `GET /boss/decisions`, `POST /boss/decisions` with
`{message_id, decision, reason, category?}`. UI: a docked "Boss mailbox" panel with an
unread badge that polls every 2 s on every tab, grouped by thread, with Release and
Quarantine on pending forwards (also the "Pending boss review" tab). Observing it changes
nothing; only the decision POST acts. The UI JS is covered by a node test with a stub DOM
(`tests/sandbox/js/mailbox.test.mjs`); it has not been looked at in a real browser.

Deviations: the protocol says quarantine is released by a human only; here the Boss decision
releases it, on explicit request. Only messages carrying a `possible_attack` signal are
forwarded (a message classed hostile without one, such as a spoofed legal notice, is not).

Re-injecting scenarios into a server that already processed them (for example `smoke`
for A1+E1 and then all six) can fail E1's "no reply" check: the benign companion's
attachment is now a duplicate of an archived document, so a duplicates relation exists and
an acknowledgement draft is produced. That is intended duplicate handling, not a defect;
reset between runs (the conformance harness does).
