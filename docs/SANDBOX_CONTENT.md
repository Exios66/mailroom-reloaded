# Sandbox content contracts (workstream M0)

The testing sandbox (addendum v2) is split in two repos. This repo owns the
**contracts**; `Exios66/mailroom-sandbox-content` owns the **content**.
Contracts live in top-level `schemas/` (JSON Schema, draft 2020-12). Where
this document and a schema disagree, the schema wins.

## Schemas

| File | Validates |
|---|---|
| `scenario.v2.json` | `scenarios/<Series>/*.yaml` (`mailroom.scenario/v2`; series enum `A-G`, `S`, `T`, AM1) |
| `registry.v1.json` | compiled client registry (`mailroom.comm.registry/v1`, the comm/v1 registry) |
| `overlay.v1.json` | one JSONL line of `email/overlay/*.jsonl` |
| `gen_spec.v1.json` | `gen/specs/*.yaml` |
| `persona_behavior.v1.json` | `personas/behavior/*.yaml` |
| `content_files.json` | CSV headers and row rules for every tabular content file; ID conventions |
| `relation_kinds.v1.json` | the eight relation kinds (**new in M0**; `unknown` is a linked_docs placeholder, not a kind) |
| `signal_kinds.v1.json` | signal/v1 kinds (**new in M0**; mirrors the content validator's `SIGNAL_KINDS`) |
| `event_kinds.v1.json` | sandbox event-log kinds (**new in M0**; addendum section 9.4) |

The first six are copied verbatim from content v0.5.0 (`f650cfd`); their
`$id`s still point at the content repo. The three enum schemas did not exist
there and are added here because the plan assigns them to M0.

## ID conventions

| Entity | Pattern | Example |
|---|---|---|
| Clients | slug | `tricountytitle` |
| Contacts | `<clientprefix>_<name>` | `tc_kalvarado` |
| Personas | `p_<client>_<role>` | `p_tricounty_real` |
| Scenarios | `<Series><n>_<slug>` (`^[A-GST][0-9]+_[a-z0-9_]+$`) | `E1_lookalike_wire_change` |
| Generation specs | `gen_<archetype>_<nnnn>` | `gen_E1_wire_change_0042` |
| Emails | `em_<series>_<nnnn>` | `em_E_0042` |
| Attachments | `att_<nnnn>` | `att_0001` |
| Relations | `rel_<nnnn>` | `rel_0001` |
| Scenario-local refs | `^[a-z][a-z0-9_]*$` | `msg_retraction` |

## Vocabulary

- Relation kinds (8): references, supersedes, duplicates, amends, answers, contradicts, withdraws, completes. Direction is normative: "a `<kind>` b".
- Signal kinds, event kinds: see the enum schemas above.
- Trust levels, attack classes, fault directives, profiles (`smoke|demo|prod_like|chaos|coverage`), gen modes (`scripted|frozen|live|loop`): enumerated inside `scenario.v2.json` / `gen_spec.v1.json`.

## Compatibility policy

- **Schema MAJOR must match.** `content.json -> schema_version` (currently `2.0`); a consumer refuses a bundle whose major differs. Minor bumps are additive.
- **Code window.** `content.json` carries `min_code_version` / `max_code_version`; the loader refuses content outside the running code's version.
- **Pinning.** `sandbox/content.lock` pins repo, tag, commit, bundle sha256, schema_version and dataset_revision. Consumers never track a branch.
- Schema changes are contract changes: they land here (M0) first, then the content repo adopts them.

## Loader layout (workstream M6)

The plan's paths map onto this repo's `src/` layout:

| Plan path | This repo |
|---|---|
| `sandbox/content/` | `src/mailroom_reloaded/sandbox/content/` (loader, compat, lock, bundle, CLI) |
| `sandbox/fixtures/smoke/` | `src/mailroom_reloaded/sandbox/fixtures/smoke/` (committed, <= 2 MB, ships in the wheel) |
| `sandbox/content.lock` | `sandbox/content.lock` at the repo root (a pin, not package data) |
| `schemas/` | repo-root `schemas/`; force-included in the wheel as `mailroom_reloaded/sandbox/schemas` |

`content.lock` fields: `repo`, `tag`, `commit`, `bundle_sha256`, `schema_version`, `dataset_revision`.
CLI: `mailroom sandbox content pull|validate|build|bump|status`. `pull` takes `--from-bundle` (sha256 verified against the lock), `--from-dir`, or `--url` (refused unless `--allow-network`). `build` validates a content dir and regenerates the smoke fixtures via its `tools/export_smoke.py`. The smoke fixtures were produced by `python3 tools/export_smoke.py --out DIR` at content v0.5.0.

## Running the content (ingress simulation)

`mailroom sandbox serve` loads the smoke set or a pulled bundle and lets you inject its scenarios and inspect the Correspondent and pipeline flows offline; see [SANDBOX_SERVER.md](SANDBOX_SERVER.md). The vendored policy copies it uses for the smoke set live in `src/mailroom_reloaded/sandbox/fixtures/policy/`.
