# Held-out scenarios: authoring protocol and measurement

A **held-out scenario** is a scenario the Correspondent was not tuned against. Its
pass rate is the only honest number for "does this generalize?", so the rules for
authoring one are stricter than for a development scenario. This document defines the
protocol, the template, and how to report the number.

Held-out scenarios use the **`H` series** (`^[A-HST][0-9]+_[a-z0-9_]+$`, e.g.
`H1_heldout_status_confirm`). The schema in `schemas/scenario.v2.json` reserves `H` for
this purpose. Tag every held-out scenario `heldout` so the harness can select the frozen
batch explicitly:

```bash
uv run --extra sandbox mailroom sandbox conformance --content <pack> --heldout
```

That runs **only** the scenarios tagged `heldout`, labels them `heldout` regardless of
position, and prints the held-out pass rate and a leave-one-family-out (LOFO) report.
Without `--heldout`, the harness falls back to the positional split (every third sorted
scenario is labelled `heldout`); that split is not a clean measure once those scenarios
have been read.

## The protocol

Write and freeze held-out scenarios without looking at results, then run them once.

**a. Write from the sources, not from the results.** Derive the expected behavior only
from the normative sources. Never open `tests/sandbox/lofo_baseline.json`,
`conformance_baseline.json`, a run log, or the Correspondent/triage source to decide what
a scenario should expect. Allowed sources:

- `protocol/delegation_matrix.csv` (pack) — `issue_class` → owner, Boss action, autonomy, notes.
- Policy files: `email/ingress_policy.yaml`, `email/recipient_policy.yaml`, `email/send_schedule.yaml`.
- `docs/SANDBOX_CONTENT.md` — ID conventions and vocabulary.
- `schemas/scenario.v2.json` — the field contract (this package).
- Reply templates under `templates/` (or `gen/templates/`) — what a reply may say.

**b. Write the expectation before the email.** Fill in `expect:` first (intent, signals,
trust, outbox, boss actions, relations, invariants), copy it into the Open Items cell of
the checklist, and only then write the message that should produce it. The expectation is
a claim about the sources; if it cannot be derived from (a), the scenario is wrong.

**c. Vary wording; include near-misses.** Do not rephrase an existing scenario. Change the
phrasing, register, and structure of the request, and include at least one **near-miss**
that is close to a different class but resolves to yours (e.g. an upset status request
that is *not* a complaint, or a payment change with passing auth). Near-misses are what
separate a real classifier from a keyword match.

**d. Coverage.** The tuning corpus should cover **≥2 scenarios per `issue_class` row** in
`delegation_matrix.csv` and **≥3 per series family** (`A`–`G`, `S`, `T`). Held-out `H`
scenarios are additional: cover **≥3** of them and at least 2 distinct `issue_class` rows,
so a single scenario cannot carry a family. Each `issue_class` needs both a
positive (correct class) and a discriminating near-miss.

**e. Single email unless it is a thread.** One message per scenario by default, so a
failure has one cause. Use multiple `timeline` entries only when the behavior under test
*is* the thread (duplicate/retraction/supersession, escalation), and say so in the title.

**f. Do not import pack contradictions.** A held-out scenario must not depend on a
contradiction already known to exist in the pack (for example, two scenarios that expect
different outcomes for the same message, or a self-test whose expectation fights the
sources). List known pack contradictions in the pack's issue tracker; do not add a
held-out scenario that would inherit them.

**g. Freeze, then run once.** Commit the scenario with `status: frozen` and an explicit
`seed`. Do not edit it after the first conformance run — a changed held-out scenario is a
new scenario and invalidates the number. If the harness exposes a bug, fix the harness or
add a *new* scenario; leave the frozen one alone.

**h. Authorship and data.** Held-out scenarios are authored by a person (or agent) who
has not tuned the Correspondent. No real client data: use the synthetic identities,
domains (`*.sandbox.invalid`), and `+1-555-01xx` numbers the pack already uses. Never paste
a real name, address, matter number, or URL into a scenario. State the author and date in
the checklist.

## Per-scenario checklist

Copy this into the scenario's PR (or a comment in the scenario file) and fill it in
**before** running conformance:

```text
Scenario: H1_heldout_status_confirm
Author / date:
Sources used (a): delegation_matrix row(s): ________ ; policy: ________ ; docs: ________
Expectation written first (b): yes/no
Near-miss described (c): ________
Coverage this adds (d): issue_class ________ ; family H count: ________
Single email (e)? yes/no ; if thread, why:
Pack contradictions checked (f): none known / listed: ________
Frozen (g): status=frozen ; seed=________
No real data (h): verified
Expected intent: ________
Expected signals/trust: ________
Expected Boss actions: ________
```

## YAML template

The complete worked example below is schema-valid against the updated
`schemas/scenario.v2.json` and is committed as
[`tests/sandbox/examples/scenario_H1_template.yaml`](../tests/sandbox/examples/scenario_H1_template.yaml).
The existing development worked example is
[`tests/sandbox/examples/scenario_A1.yaml`](../tests/sandbox/examples/scenario_A1.yaml).

```yaml
# Held-out scenario template (docs/HELD_OUT_SCENARIOS.md).
# Write the expectation FIRST, then the email, from the sources only. H names are the
# frozen held-out batch; tag them `heldout` so `mailroom sandbox conformance --heldout`
# can measure them. Replace every value below before freezing; it is a shape, not content.
name: H1_heldout_status_confirm
title: Counsel asks for a status confirmation on in-flight matter HP-2026-0417
seed: 9101
profile: smoke
gen: scripted
transports: [sim, agentmail]
status: draft
tags: [heldout, status]
timeline:
- at: "00:02"
  client:
    persona: p_harlow_counsel
    channel: email
    claimed_from: dwhitcomb@harlowpryce.sandbox.invalid
    auth: {spf: pass, dkim: pass, dmarc: pass}
    template: status_inquiry
    vars: {matter_ref: HP-2026-0417, matter_description: Schedule C indemnification review}
expect:
  intent: status_request
  signals:
  - {kind: status_request, priority: normal, within: "00:05"}
  trust: {sender_level: verified}
  outbox:
  - {intent: status_request, state: draft, to_sender: true}
  boss_actions: [ack_signal, approve_outbound]
  overblocking: {benign_hard_actions: 0}
  invariants: [audit_chain_ok, no_stuck_docs, fast_path_two_calls, comms_offpath]
```

## Reporting the held-out number

Run the frozen batch once and report exactly what the harness prints:

```bash
uv run --extra sandbox mailroom sandbox conformance \
  --content <extracted pack> --heldout --json /tmp/heldout.json --slim
```

Report, together:

- **Held-out pass rate** — `mailroom sandbox conformance --heldout`: the `heldout` line of
  the per-scenario table (`pass / (pass+fail+not_run+shed)`).
- **Macro mean held-out rate** and **micro pass rate** from the LOFO table below it (the
  macro mean weights families equally; micro weights scenarios equally). `n/a` means the
  batch was empty.
- **Counts**, not just the rate: `pass`, `fail`, `not_run`, `shed`, and the total, plus
  the failed-check histogram.
- **What was frozen and when** — commit SHA, author, and the fact that no scenario changed
  since. A rate without that provenance is not a held-out measure.
- **The positional number only as context**, clearly labelled: `index % 3` is optimistic
  once those scenarios have been read during development.

Do not average the held-out rate with tuning-family rates, and do not report the
positional split as "held-out" once `--heldout` exists.
