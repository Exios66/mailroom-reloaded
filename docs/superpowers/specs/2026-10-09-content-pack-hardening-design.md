# Content pack hardening: design notes (brainstorm output)

- **Date:** 2026-10-09
- **Scope:** what `Exios66/mailroom-sandbox-content` still needs so that `mailroom-reloaded` can consume it reliably and so that bad input produces an error report, never a crash or a silent pass.
- **Plan:** the work items are workstreams **K-00..K-08** in [`../plans/2026-10-09-mailroom-core-plan.md`](../plans/2026-10-09-mailroom-core-plan.md) (Phase 4b). This file holds the reasoning, the evidence and the options that were rejected. It is a spec, not a second plan.
- **Method:** static read of both repos and all open PRs and their review threads; `bash tools/ci.sh` on content `main`; a bundle rebuild under two compressor versions; every content file validated against reloaded's schemas; the real consumer loader run on the raw checkout, the extracted bundle and the H-series branch; 28 injected faults against the validator.
- **State at audit:** content `main` `f650cfd`; reloaded `main` `44c8b0f` (core plan merged); open PRs: content #5, #6; reloaded #23, #44, #45.

---

## 1. Problem

The pack is complete enough to run (88 scenarios, CI green, loads clean). What is missing is the layer that makes it *trustworthy as a pinned dependency*:

1. The pin cannot be independently reproduced.
2. The pack contains expectations that contradict each other, so some failures in reloaded are not code failures.
3. The tooling that guards the pack has crash and silent-accept paths.
4. Content CI cannot see a consumer-side contract change until after a pin.
5. The plan's own description of schema drift was backwards.

## 2. Evidence

### 2.1 Baseline (what works)
| Check | Result |
| --- | --- |
| `bash tools/ci.sh` on `f650cfd` | exit 0; 88 scenarios (A17 B8 C11 D6 E13 F5 G12 S10 T6), 0 errors, 1 WARN (32 scenarios without a gen spec), 118 unit tests OK, smoke export 28 files / 29,730 bytes, strata match the catalog |
| 142 pack files (scenarios, gen specs, persona behaviors) vs **reloaded's** schemas | 0 failures |
| Reloaded's `load_content` on the raw checkout and on the extracted bundle | 88 scenarios, 14 personas, 40 gen specs, 0 errors |

### 2.2 Findings
| # | Finding | How measured | Severity |
| --- | --- | --- | --- |
| F1 | Bundle sha256 is not reproducible across `zstandard` versions: 0.25.0 gives `7a32e86e...` (= lock), 0.23.0 gives `c6dafadb...` on the same commit and tar. Lock comment already says the pinned build is unpublished. | Two builds in separate venvs | High: the pin is the whole trust model |
| F2 | 4 validator crashes (BOM, short row, NUL, corrupt `ids/ranges.yaml`) and 2 silent accepts (extra cells, duplicate key). 21 of 28 faults fail cleanly. | Fault harness, `tools/fault_inject.py` | High |
| F3 | Contradictory expectations. Verified: S1/S2/S3 same template and intent, `fyi` priority normal/normal/high; D6 `outbox: []` vs G4/G7 draft reply for the same intent. Reported by reloaded #23, not yet verified here: A9, T5, T6, C4, E7, A4, G9, F5, S5-S10. | Read the YAML; #23 `docs/SANDBOX_SERVER.md` root-cause table | High: inflates "fail" in reloaded |
| F4 | Content branch `feat/heldout-h-series` passes content CI but fails reloaded `main`'s loader (28 name-pattern errors). | Ran the real loader on the branch | Medium: ordering hazard |
| F5 | The core plan's X-03 says content's `gen_spec` and `persona_behavior` schemas are stricter and should be upstreamed. The diff shows the opposite. | Structural diff of both copies | Medium: following the plan would loosen the contract |
| F6 | `tools/ci.sh` strata-drift step needs network and is the only drift gate; `release.sh` can be told to skip it. | Read scripts | Medium |
| F7 | Loader (reloaded) lets file and YAML parse errors propagate instead of reporting them. | Per its docstring; not exercised | Low-medium |
| F8 | Boss-decision lifecycle in the sandbox is not recoverable/idempotent; a `legitimate` decision can release a hard-quarantined handoff. | CodeRabbit reviews on #23 and #44 (not independently verified) | Medium, reloaded-side |

### 2.3 Things checked and found fine (do not spend time here)
Phone and brand leak scans (injected a non-555 number and a real domain: both caught); a lookalike domain placed in the registry source (caught); template syntax error, undefined variable, missing template (caught); attachment tamper and missing file (caught); symlink pointing outside the repo (caught); YAML alias bomb (caught); CRLF line endings (correctly accepted).

## 3. Options and decisions

### 3.1 Making the pin verifiable (F1)
| Option | Why / why not |
| --- | --- |
| **A. Pin `zstandard`, treat the published asset's bytes as the authority, add `tar_sha256` for audits** (chosen) | Smallest change; the lock keeps its six fields; a downloader verifies exactly what was published; an auditor can still prove the content is identical via the uncompressed tar. |
| B. Put the digest of the uncompressed tar in the lock | Compressor-independent, but changes `content.lock` fields; the consumer's `ContentLock` rejects unknown fields, so it needs a coordinated contract change in both repos for little gain. |
| C. Switch to gzip | gzip output is stable across zlib builds in practice, but it loses the zstd size win and still is not formally guaranteed. Rejected. |
| D. Do nothing; "published asset is the truth" | True, and part of A, but leaves rebuilds unreproducible and the docs claim determinism that does not hold. |

### 3.2 Hardening the validator (F2)
| Option | Why / why not |
| --- | --- |
| **A. One strict CSV reader, guarded YAML loads, outer exception guard that converts to `ERROR internal`, plus the fault suite as unit tests** (chosen) | Fixes the class, not the four instances; the suite prevents regression. |
| B. Patch the four crash sites only | Leaves the next malformed file to crash somewhere else. |
| C. Replace hand-written checks with schema-only validation | Cross-file rules (references, hashes, leak scan, coverage) cannot be expressed in JSON Schema; already decided in content CD6. |

### 3.3 Contradictory scenarios (F3)
The authority must be something other than the system under test, otherwise the pack just encodes the Correspondent's current behaviour and stops being a test. Decision (D12): `protocol/delegation_matrix.csv` and the protocol decide; the owner breaks ties. A lint (same template + same intent must agree on outbox and priority unless annotated `contrast:`) keeps new contradictions from entering, including from the H-series batch.
**Dependency on in-flight work:** the owner reports a PR is waiting that patches these scenarios. None was visible in either repo when this was written (content #5 only adds H1-H28; reloaded #23 says it changes no content). K-00 makes reconciliation the first step so the work is verified against the K-03 table instead of duplicated.

### 3.4 Where the plan text lives
The core plan states it is the only live plan and that `plans/` holds exactly one file. The K-series is therefore added to that file as Phase 4b with a dated note, and this spec carries the rationale. No new plan file was created.

## 4. Risks and mitigations
| Risk | Mitigation |
| --- | --- |
| K-02 (strict CSV) rejects a file that is currently accepted and needed | Run the new reader over the whole pack first; the acceptance test is "pack still validates with 0 errors" before any fault case. |
| K-03 decisions are made from a table that includes unverified rows (#23) | First step in K-03 is to verify each row against the YAML; unverified rows are not edited. |
| Lint in K-03 flags legitimate contrasts (e.g. same wording, different sender trust) | `contrast:` annotation with a required reason string; start as WARN, promote to ERROR after the table is applied. |
| Pinning `zstandard` blocks contributors on other versions | Refusal applies only under `--release`; unit tests and ordinary builds are unaffected. |
| Plan goes stale (a parallel agent is still pushing; PRs changed three times during this audit) | K-00 requires `git fetch --all --prune` and a re-check before acting on any PR-dependent row. |

## 5. Explicitly out of scope
Phase 3 items that need external access (dataset join, relation truth, frozen email generation, OpenRouter key): unchanged, they stay C-01..C-03. Promotion of scenarios to `frozen` stays a human step (C-05). Reloaded Correspondent-v2 triage (R-03) is not touched. No pipeline code is changed by any K-item except the loader item K-08.

## 6. Open questions for the owner
1. Which PR is the scenario-patch PR, and is it on a fork or a branch not visible to this session? (K-00)
2. Confirm D11 (asset bytes are the verification authority) before K-01 starts.
3. Confirm D12 (matrix decides contested expectations).
4. The content repo is still public while the original plan called for private (content issue #2, "visibility decision"); unchanged by this work, but it affects whether dataset-derived manifests can ever be bundled.

## 7. Verification commands used (reproducible)
```bash
bash tools/ci.sh                                   # content main
python3 tools/build_bundle.py --out <dir>          # run under two zstandard versions
python3 -I tools/fault_inject.py <repo> <scratch>  # 28 mutations; run from the content branch claude/upbeat-euler-85ifix
```
The loader probe stubbed the parent packages of `mailroom_reloaded.sandbox.content` so the module could be imported without the heavy runtime dependencies, then called `load_content(<dir>)`.
