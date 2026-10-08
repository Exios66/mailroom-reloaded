# Evaluation

`mailroom eval` scores pipeline runs against the blind/ground-truth corpus
`Lucius-Morningstar/mailroom-dataset` and writes one row per document to the
SQLite `eval_docs` table. This page covers the dataset contract, the train/test
discipline, the two evaluation postures, the metrics, and the SAND-37 cards.
CLI flags are defined in `cli.py:105-144`.

## Dataset

| Item | Value | Source |
| --- | --- | --- |
| Repo | `Lucius-Morningstar/mailroom-dataset` | `eval/dataset.py:41` |
| Default revision | `ed7576b6` | `eval/dataset.py:42` |
| Blind config | `default` — `filename`, `doc_text`, `prompt`, `metadata`; **no labels, no hash** | `eval/dataset.py:4-6` |
| Ground-truth config | `ground_truth` — labels in the `gt_fields` JSON blob plus `expected`, `expected_subclass`, `content_sha256` | `eval/dataset.py:6-8` |
| Join key | `filename` | `eval/dataset.py:352-363` |
| Split default | `test` (`--split`) | `cli.py:119` |

`load_split` verifies `content_sha256 == sha256(doc_text)` for every blind
document and raises `DatasetIntegrityError` on a mismatch, so a corrupted or
truncated document cannot silently reach the pipeline (`eval/dataset.py:8-9`,
`eval/dataset.py:208-231`, `eval/dataset.py:366-381`). The blind side is a
frozen `BlindDoc(filename, doc_text, content_sha256)` with no label fields
(`eval/dataset.py:62-69`); labels live in `GroundTruth` (`eval/dataset.py:71-84`)
and are reachable from only two places — the eval-only `grade` step and the
`get_ground_truth` tool (`eval/dataset.py:11-14`, `pipeline/flow.py:260-278`).

`--local-dir` reads local `default.jsonl` / `ground_truth.jsonl` exports
(offline/tests, `eval/dataset.py:317-322`); otherwise `datasets.load_dataset`
is used behind the `eval` extra (`eval/dataset.py:325-349`). Without the extra,
a live load raises a message naming `mailroom-reloaded[eval]`
(`eval/dataset.py:329-335`).

### Sampling

`sample` draws up to `--per-class` documents per class and is **nested**: each
class's documents are shuffled by a seed derived from `(seed, class)`, so the
`n=20` draw is always a prefix of the `n=50` draw for the same class and seed
(`eval/dataset.py:387-421`). Class order follows the taxonomy; unknown classes
follow, sorted. `select_graded` picks the judge sample deterministically:
`rate <= 0` grades nothing, `rate >= 1` grades everything, otherwise the first
`round(rate·n)` by seeded hash (`eval/runner.py:131-149`).

## Train / test discipline

The learned gate and the sorter temperature calibration are fitted **only on
`split="train"` rows**; `train_gate._check_train` raises `ValueError` for any
missing or non-`train` split, and KPIs are reported on `test` only
(`eval/train_gate.py:1-5`, `eval/train_gate.py:47-55`). `mailroom eval` defaults
to `--split test` (`cli.py:119`).

Leakage is also checked structurally: the blind `BlindDoc` carries no labels, the
flow reaches ground truth only through `EvalContext` (`eval/dataset.py:86-96`),
and `tests/helpers.assert_no_gt` asserts no ground-truth value appears in any
recorded sorter/specialist request (`tests/helpers.py:50-62`). A BERT-training
overlap helper exists (`bert_manifest_overlap`, `eval/dataset.py:302-311`) for
the spec §5 leakage check; note it is a **helper only** — the runner does not
call it yet.

## Running an evaluation

```bash
# Full pipeline on the test split (default 20 docs/class):
uv run mailroom eval

# Full pipeline, a class subset, machine-readable run id:
uv run mailroom eval --classes contract,merger_agreement --per-class 10
# prints the run_id (hex, 12 chars) to stdout

# SAND-37 specialist cell against a GPU endpoint:
uv run mailroom eval --mode specialist_cell --gpus 2 --concurrency 32 \
  --revision ed7576b6 --prompt-set sand37
```

`mailroom eval` flags and defaults (`cli.py:105-144` → `EvalConfig`,
`eval/runner.py:109-128`):

| Flag | Default | Meaning |
| --- | --- | --- |
| `--revision` | `ed7576b6` | Dataset revision. |
| `--per-class` | `20` | Documents sampled per class (nested). |
| `--seed` | `42` | Sampling + judge-sampling seed. |
| `--classes` | `""` (all) | Comma-separated class filter. |
| `--concurrency` | `8` | Max in-flight documents (semaphore). |
| `--posture-label` | `pipeline` | Label for the posture. |
| `--gpu` / `--gpus` | `L4` / `1` | Cost-card GPU type and count. |
| `--prompt-set` | `frozen_v1` | `frozen_v1` or `sand37`. |
| `--merger-mode` | `frozen` | `frozen` or `dagger`. |
| `--mode` | `pipeline` | `pipeline` or `specialist_cell`. |
| `--judge-sample-rate` | `1.0` | Fraction graded by the judge (seeded). |
| `--split` | `test` | `test` (KPIs) — never use `train` for reported numbers. |
| `--local-dir` | unset | Offline JSONL directory. |
| `--gpu-usd-per-hour` | `0.80` | GPU-hour price for the cost card. |

### `pipeline` vs `specialist_cell`

- **`pipeline`** runs the full `MailroomFlow` — ingest, BERT, sorter, gate,
  specialist, verify/boss as routed — under an `asyncio.Semaphore(concurrency)`
  (`eval/runner.py:371-422`, `eval/runner.py:461-481`). If a document is selected
  for grading, an `EvalContext` is attached so the `grade` step runs after
  archive (`eval/runner.py:384-389`, `pipeline/flow.py:646-655`).
- **`specialist_cell`** feeds the **ground-truth class** straight to `extract`,
  skipping sorter and gate, as SAND-37 does; results are ungraded
  (`eval/runner.py:425-458`). In `sand37` prompt-set mode tools are forced off
  (`agents/specialists.py:122-129`).

Every document gets one `eval_docs` row with identity, stage outputs, confidence,
`schema_valid`, `parse_error`/`error_kind`, `length_capped`, latency, token
counts, `calls`, `graded`, the judge grade, `gate_features` and `route_trail`
(`eval/runner.py:46-101`). A per-document exception is recorded as an error row
and never aborts the run (`eval/runner.py:404-411`).

## Metrics

`eval/metrics.py` computes three KPIs from `eval_docs`-shaped rows:

- **`sorter_kpis`** — exact match, primary accuracy, subclass accuracy (overall
  and conditional on a correct primary), per-class confusion matrix, `by_path`
  (`SUBCLASS_ONLY`/`FULL`), `resort_rate`, BERT fast-path/defer rates, and ECE
  (`eval/metrics.py:284-321`).
- **`specialist_kpis`** — field precision/recall/F1/F2, micro F1 (pooled TP/FP/FN),
  mean per-document F1, suite mean/sd/min/max, schema-valid rate, parse errors,
  CUAD clause block, MAUD block, and `judge_scorer_agreement`
  (`eval/metrics.py:413-476`).
- **`gate_kpis`** — decision mix per stage and agreement with the dataset's
  `retry_expected` / `review_expected` / `expected_stage`
  (`eval/metrics.py:482-531`).

Field counts follow the dojo `extraction_binary_metrics` table with match
threshold **0.5** (`eval/metrics.py:5-15`, `eval/metrics.py:41`):

| Ground truth | Prediction | Outcome |
| --- | --- | --- |
| non-empty | matches (score ≥ 0.5) | TP |
| non-empty | non-empty but below 0.5 | FP + FN |
| empty | non-empty | FP |
| non-empty | empty | FN |
| empty | empty | not counted |

## Cost

Cost is computed two ways and both return the same four keys so one card table
renders either (`eval/cost.py:3-16`, `eval/cost.py:70-120`):

- `gpu_hour` (default) — `busy_gpu_usd = wall_s/3600 × usd_per_hour × gpus`;
  local and Modal runs.
- `per_token` — `cost_models` in `taxonomy.yaml` applied to prompt/completion
  tokens; OpenRouter runs.

A missing input yields `None`, never a fabricated `$0`. `token_split` fits
`prompt_tokens = I·calls + chars/r` and reports `instruction_per_call` and
`chars_per_token` (`eval/cost.py:131-210`).

## Cards (SAND-37)

`eval/cards.py` emits a `mailroom.card/v1` dict (`CARD_SCHEMA`,
`eval/cards.py:29`) with the same ten blocks as the sandbox card
(`eval/cards.py:32-43`):

`conditions`, `cost`, `documents`, `engine_telemetry`, `latency`, `quality`,
`throughput`, `time`, `tokens`, `concurrency`.

- `build_card(run_id, doc_type)` reads `eval_docs` rows for the run (optionally
  filtered by class) and fills every block (`eval/cards.py:137-315`).
- `render_card_md(card)` renders the SAND-37-style Markdown table; a metric the
  rows did not capture renders as `not captured`, never a fabricated zero
  (`eval/cards.py:341-473`).
- `build_master(run_ids)` aggregates cells into the posture / serving-efficiency /
  quality-and-cost tables (`eval/cards.py:523-636`).
- `engine_telemetry` per replica (requests, length finishes, preemptions,
  prefix-cache hit rate, mean TTFT, KV-cache usage) comes from
  `eval/vllm_telemetry.py` (`eval/cards.py:111-131`).

**Where cards land.** Card JSONs are served by the API from
`<base_dir>/runs/<run_id>/cards/*.json` (`api/app.py:249-260`) and the `/ui`
runs page reads that endpoint. `mailroom card --run-id RUN [--doc-type CELL]`
writes `<out>/<run_id>/cards/card-<cell|all>.{json,md}`; `--master` (or two or
more `--run-id`s) writes `<out>/master.{json,md}` via `build_master`
(`cli.py` `card`, `eval/cards.py:137,341,523`). The behavioural conformance suite
writes `<out>/conformance-<provider>.{json,md}` (default `runs/conformance/`) via
`mailroom conformance --provider X` (`cli.py` `conformance`, plan Task 24).

**Judge.** The judge grades each selected document against ground truth in the
`grade` step (`agents/judge.py`, `pipeline/flow.py:260-278`) and runs separately
from the deterministic scorer; `judge_scorer_agreement` reports how often the two
agree (`eval/metrics.py:383-410`). The deterministic scorer is authoritative for
the KPIs.

## Fitting the gate and calibration

```bash
# Fit the learned route gate from JSONL feature rows (split="train"):
uv run mailroom train-gate --rows rows.jsonl --out models/route_gate.json

# Fit sorter temperature scaling instead:
uv run mailroom train-gate --rows rows.jsonl --calibration --out models/calibration.json
```

`--rows` is required and must exist (`cli.py:147-165`). Calibration rows are
`{split, provider, model, doc_type, confidence, correct}`; gate rows are
`{split, stage, doc_type?, confidence, attempts, bert_confidence, bert_margin,
bert_window_agreement, schema_valid, field_coverage, length_capped,
retry_expected|review_expected}` (`eval/train_gate.py:7-16`). Fitting maps to
the layouts the runtime reads: nested temperatures for `models/calibration.json`
and `{stage: {features, coef, intercept, threshold}}` for
`models/route_gate.json` (`eval/train_gate.py:17-22`). Training requires the
`dev` extra (scikit-learn, `pyproject.toml:48`).

## Cross-links

- [ARCHITECTURE.md](ARCHITECTURE.md) — the pipeline the eval drives.
- [CONFIGURATION.md](CONFIGURATION.md) — `cost_models`, `confidence`, model maps.
- [OPERATIONS.md](OPERATIONS.md) — where runs, traces and cards are observed.
- [TESTING.md](TESTING.md) — `tests/eval` and the parity suite.
- `deploy/README.md` — a SAND-37-style posture run against Modal.
