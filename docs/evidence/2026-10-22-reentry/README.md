# Reentry status and evidence — 2026-10-22

This is the missing evidence/status record referenced by #105, created during
the authorized reentry on 2026-10-22 (UTC). It distinguishes shared-checkout
implementation, open PRs and merged/released work; it is not a completion card
or an eval result. The release remains **v0.4.2**; no new release is being cut.

## Scope and provenance

- Canonical checkout: `/Users/luciusjmorningstar/Downloads/mailroom-reloaded`.
  This docs pass runs under Codex on `feat/tui-p6b1-b4`, without git writes,
  issue changes, provider calls, deployments or nested agents.
- The [v1 plan](../../superpowers/plans/v1.md) is the only active plan. The
  [core plan](../../superpowers/archive/plans/2026-10-09-mailroom-core-plan.md)
  and [TUI wiring plan](../../superpowers/archive/plans/2026-10-10-pipeline-tui-wiring.md)
  remain archived closure records, not revived workstreams.
- Reuse #105's preserved recon and the
  [historical work log](../../superpowers/archive/DISCUSSION_BOARD.md).
  Earlier implementation, code-analysis, test-suite-auditor and docs-board
  contributions under Claude Code retain their original attribution. Their
  historical test counts, branch inventory and source traces are not fresh
  proof from this docs pass.
- Preserve the prior `claude/cool-wozniak-o044u4`, `docker-patch-02` and
  `feature/ingress-sandbox` research/work. Do not blindly merge or discard them.
  No fresh fetch/reachability audit or branch-retirement claim is made here.

## Repository / PR versus main

| Item | Status on 2026-10-22 | Remaining boundary |
| --- | --- | --- |
| #109 / PR #116 | Scoring update implemented in open `feat/scoring-v0.22.0` PR against `main`; this is not a merged or released update. Parent reports the reviewer independently verified all 11 upstream file hashes. | Keep `Refs #109`, not `Closes #109`: merge/release checklist remains open. This docs pass did not repeat the hash review or parity gates. |
| Recovery/replay PR #115 | Open `fix/pipeline-recovery-and-replay` PR against `main`. Reviewer found an approve/correct audit-replay `processing` regression; parent will fix it. | Not merge-ready. Fix the regression and rerun the relevant recovery/review/replay tests and adversarial review before any merge claim. |
| #64 B1–B4 | Code committed as `35626fca9dcc9bd7da31f3256010569df14b95f3` on `feat/tui-p6b1-b4`. Parent executed final local gates and both browser checks; [command evidence](../../demo/p6b1-4/README.md) includes raw logs, four screenshots and a generated source/hash manifest. Independent review's readiness deadline finding is fixed with a red/green regression. | PR review and merge remain pending. No main-completion claim; closure still requires criterion-to-merged-SHA evidence. |
| #65 B5–B10 | Next separate per-command PR series; no completion asserted. | Keep control default-off, preserve the owner-decision boundary and prove each command's auth/control/error contract. |
| #108 | Closed. Parent supplied a fresh targeted regression result below. | Do not reopen or duplicate the fix; the result is attributed to the parent, not rerun here. |
| #105 | Integration/eval tracker remains open. | No first full-dataset eval, calibrated production result or live/full-stack proof is asserted here. |

The PR states above were independently read in this docs pass with
`gh pr view 115 --json number,state,isDraft,headRefName,baseRefName,title` and
the equivalent command for `116`: both returned `state: OPEN`, base `main`.
The reviewer findings, 11-file hash verification and next-fix intent are
parent-supplied evidence, not independently reproduced by this docs pass.

## Fresh parent-supplied regression evidence

For closed #108, the parent reports executing:

```text
PYTHONPATH=src .venv/bin/pytest -p no:cacheprovider tests/obs/test_pager.py -q -k test_pager_releases_lock_when_smtp_credentials_are_lost
9 passed, 47 deselected in 0.19s
```

This is the targeted pager credential-loss tier only, not a full-suite result.

## #64 evidence and closure boundary

- B1–B4 remain unticked in the archived wiring plan until merge evidence is
  available. A PR must say `Refs #64`, not `Closes #64`.
- Captured command screenshots and their generated manifest belong under
  `docs/demo/p6b1-4/`, specifically `docs/demo/p6b1-4/manifest.json`. Preserve
  `docs/demo/manifest.json` and the older release gallery. This scoped choice
  reconciles the archived sub-plan's screenshot policy with the core plan's
  D8 evidence convention; infrastructure/gate logs remain under `docs/evidence/`.
  A path named here is a plan, not proof that the capture exists or passed.
- B1's archived `sandbox_url` wording is historical: the current main console
  does not restore that off-main sandbox surface. Prometheus opening remains
  conditional on the server publishing its URL. Do not silently infer either
  capability from the old checklist.
- The parent must append actual gate commands, result lines, source commit,
  browser/capture results and remaining limitations after running them. No
  screenshot, browser, CLI/export parity or application-suite pass is claimed
  by this README.
- At closure, post criterion → evidence → PR → full merged SHA mapping for
  every numbered #64 criterion. Use the actual archived plan paths linked
  above; then reconcile the B1–B4 entries and the core plan's Phase 6 status.
  An open PR or a working-tree implementation is not that mapping.

## Bounded backlog disposition

These carry forward the bounded recon, not a new full source audit. No issue
was closed, relabelled or otherwise altered by this docs pass.

| Issue(s) | Actionable disposition / blocker |
| --- | --- |
| #109 | Implementation is already in PR #116; do not restart vendoring. Preserve reviewer hash evidence and finish the separate merge/release checklist without cutting a release in this mission. |
| #8 | Reconcile shipped Jev modules, calibration tooling, CLI, docs and current offline gates with the original criteria; do not rebuild the integration. Production calibration/eval remains separate #105 work. |
| #50 / #51 | Preserve off-main sandbox fixes and verify provenance/current applicability before salvage. Replies follow verified fixes. Historical write-permission failures do not establish today's credential state. |
| #66 | Resolve R-05/D18 (emit eval gauges versus amend docs), then remaining trace/dashboard wiring and runtime evidence; #64 links and #65 metrics/eval dependencies are not completion proof. |
| #67 | Reuse historical Docker evidence rather than claim a new smoke. Reconcile residual criteria and the obsolete hosted-workflow requirement with current no-Actions policy. |
| #68 | Owner decisions, D17 price-source choice, #65 metrics dependencies and separately authorized live deployment/spend proof remain outstanding. No GPU/Modal run is claimed. |
| #69 | Existing capture/release-note groundwork is not a full command walkthrough or generated-reference proof. B1–B4 captures alone do not close F1–F4. |
| #70 | Needs separately authorized live local-provider conformance, a saved card and measured pass-rate summary. No card is invented here. |
| #72 | Fresh fixture byte/hash comparison remains required. Prior read-only alert query found no open chromadb alerts but 12 other open alerts; broader dependency triage belongs separately, not blanket dismissal. |
| #73 | Preserve historical headless evidence and its explicit headed/human-checklist limitation. No fresh browser run or duplicate implementation here. |
| #74 | Owner-controlled Supabase staging execution and redacted DDL/grant/trigger/header evidence remain required; no staging credentials or run in this mission. |
| #76 | Board archival already exists. Reconcile historical wording and residual owner-approved issue/branch/content actions; no branch deletion or issue closure here. |
| #79 | Citation request lacks a concrete target/metadata contract; establish the dataset citation location and acceptance criteria before editing. |
| #105 / #108 | Keep #105 as the incomplete integration tracker; retain closed #108 and the attributed fresh regression result above. |

## Evidence classifications and limitations

- **Harness:** doc/reference audits, test-command provenance, browser tooling
  and the no-hosted-Actions policy. A successful reference audit is not an
  application or integration gate.
- **Vendored upstream:** #109/PR #116 hash/parity evidence and preserved sandbox
  content provenance. Never hand-edit pinned vendor bytes to make a test pass.
- **Operator/env:** live-provider availability/authorization, Docker runtime
  proof, Modal credentials/spend, Supabase staging, owner decisions and branch
  retirement. Historical permission failures are carried-forward observations,
  not fresh access tests.
- **Model:** full-dataset quality, cost, calibration and live conformance remain
  unmeasured here. No accuracy, F1, calibrated probability or cost figure is
  substituted for the v1 plan's `TBD` values.

Recovery/replay code fixes and the review regression remain open software work;
neither source inspection nor these evidence classifications establish that
they are fixed. Historical browser and Docker results remain available in the
[2026-10-10 TUI record](../2026-10-10-tui-live-check/README.md) and
[Docker record](../2026-10-10-docker-verification/README.md), with their original
dates, environments and limitations.

## Docs-only validation — 2026-10-22

This docs sub-agent ran a Python standard-library reference/status audit:

```text
PASS: 10 new local documentation references resolve; B1–B4 unticked; active-plan files unchanged; attributed status preserved
```

Re-run the reference portion from the repository root:

```bash
python3 - <<'PY'
import re
import subprocess
from pathlib import Path

paths = [
    Path('docs/superpowers/archive/plans/2026-10-10-pipeline-tui-wiring.md'),
    Path('docs/superpowers/archive/plans/2026-10-09-mailroom-core-plan.md'),
    Path('docs/evidence/2026-10-22-reentry/README.md'),
]
checked = 0
for path in paths:
    content = path.read_text()
    if path.name != 'README.md':
        diff = subprocess.check_output(['git', 'diff', '--', str(path)], text=True)
        content = '\n'.join(line[1:] for line in diff.splitlines()
                            if line.startswith('+') and not line.startswith('+++'))
    for target in re.findall(r'\]\(([^)]+)\)', content):
        if '://' in target or target.startswith('#'):
            continue
        assert (path.parent / target.split('#')[0]).resolve().exists(), target
        checked += 1
print(f'PASS: {checked} new local documentation references resolve')
PY
git diff --check -- docs/superpowers/archive/plans/2026-10-10-pipeline-tui-wiring.md docs/superpowers/archive/plans/2026-10-09-mailroom-core-plan.md docs/evidence/2026-10-22-reentry/README.md
```

The scoped whitespace check exited 0. The reference count describes the
uncommitted diff at audit time; after commit the diff-based portion changes.
Existing active-plan files, including the dated operator SMS sub-plan, were
preserved. These checks validate docs, not code, browser behavior or merge
readiness; no application tests were run by this follow-up docs pass.
