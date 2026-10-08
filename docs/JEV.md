# Jev (TypeSafe System One)

Jev is an **opt-in, probabilistic decision model** — the TypeSafe *System One*
— that can back the pipeline's route gate (`agents/gate.py`). It is **not a
chat/generation LLM**: it returns typed, calibrated *answers* to structured
questions rather than free text. When Jev is off (the default) the pipeline is
byte-for-byte unchanged; when it is on **and calibrated**, `load_gate()` prefers
a `JevGate` over the deterministic `BandGate` / `LearnedGate`.

Source of record: tracking issue
[Exios66/mailroom-reloaded#8](https://github.com/Exios66/mailroom-reloaded/issues/8).
Implementation: `agents/jev.py` (381 lines), `eval/jev_calibration.py` (138
lines), the `jev` command group in `cli.py:246-327`, the `jev:` block in
`config/taxonomy.yaml:142-160`, and `settings.py:146-271`.

## What Jev is

`JevClient.ask` sends one envelope and parses an `answers` mapping
(`agents/jev.py:213-228`; request `{"model", "state", "questions"}` at
`agents/jev.py:217`; response parsed at `agents/jev.py:173-188`). Each question
is built by one of three builders and each answer is a typed `JevAnswer`
(`agents/jev.py:76-112`, `agents/jev.py:143-170`):

| Type | Shape | Meaning |
| --- | --- | --- |
| `choice` | `{type, choice, probabilities, confidence}` | One option plus the per-option probability distribution and a confidence. |
| `noul` | `{type, noul}` | A single `P(yes)` probability (`agents/jev.py:20`, `agents/jev.py:97-105`). |
| `score` | `{type, score, legend}` | A probability-weighted position with a label legend (`agents/jev.py:108-112`). |

`confidence` is **derived from the distribution concentration, not from
per-item correctness**: when the server omits it, the client falls back to the
probability of the chosen option, else the maximum probability
(`agents/jev.py:151-156`). It is not a separately calibrated decision head — the
issue flags that "confidence is the top option probability, not a separately
calibrated decision head". Treat it as a raw signal that the calibration
(§ Calibration) must re-scale before the gate trusts it.

The question builders are:

- `choice(name, instructions, criteria)` — `criteria` maps **each option → a
  description** (`agents/jev.py:90-94`).
- `noul(name, instructions, criteria=None)` — missing criteria normalizes to
  `{}` (`agents/jev.py:97-105`).
- `score(name, instructions, criteria)` — `criteria` is an ordered list of
  score labels (`agents/jev.py:108-112`).

The gate asks exactly two questions: a `route` **choice** over
`proceed | retry | verify | boss | human_review`, and an `escalate` **noul**
"should this be escalated to a human reviewer?" (`_jev_questions`,
`agents/jev.py:278-295`).

## Transports

`MAILROOM_JEV_PROVIDER` selects one of three transports
(`settings.py:148`, `settings.py:151-165`, `settings.py:254-263`). All three use
the same typed-decision HTTP shape; only the endpoint and default model differ.

| Provider | Endpoint | Default model | API key | Context |
| --- | --- | --- | --- | --- |
| `openrouter` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` | `OPENROUTER_API_KEY` | 32K (issue #8) |
| `typesafe` | `POST https://api.typesafe.ai/v1/systemone` | `jev-latest` | `TYPESAFE_API_KEY` | 64K — **not confirmed in code (see below)** |
| `local` | `POST http://127.0.0.1:8090/v1/systemone` | `jevk5` | optional | self-hosted JevK5 |

- **OpenRouter Decisions API** (`settings.py:152-156`). Not OpenAI-compatible;
  the issue notes "input `$0.042/M`, output free; 32K ctx".
- **TypeSafe native** (`settings.py:157-160`). The 64K context figure appears
  only in this documentation's brief, not in the code or issue — flagged
  unconfirmed.
- **Local / offline `jevk5`** (`settings.py:161-164`). A self-hosted JevK5
  server with the same typed-decision shape and one forward pass. The JevK5
  runtime internally reads letter logits as `softmax(logit / 1.22)` per the
  model card in issue #8; that 1.22 is the runtime's own softmax divisor, **not**
  the request `temperature` mailroom sends (which defaults to `1.0`,
  `settings.py:167-172`, set via `MAILROOM_JEV_TEMPERATURE`/`JEV_TEMPERATURE`).
  `1.22` is not present anywhere in this repository — flagged as issue-sourced.

Requests carry `Authorization: Bearer <api_key>` only when a key resolves; the
local provider may run keyless (`agents/jev.py:218-221`,
`settings.py:229-243`).

**Key resolution order** (`_jev_api_key`, `settings.py:229-243`; mirrored in
`.env.example:32-35`):

1. `MAILROOM_JEV_API_KEY` (via `_jev_env("API_KEY")`)
2. `JEV_API_KEY`
3. `TYPESAFE_API_KEY`
4. `OPENROUTER_API_KEY`
5. `settings.openrouter_api_key`

## How it plugs in

Routing is an opt-in swap in `load_gate` (`agents/gate.py:169-180`):

```python
jev = load_jev_gate(load_taxonomy())
if jev is not None:
    return jev
# else: LearnedGate if models/route_gate.json exists, else BandGate
```

`load_jev_gate` returns a `JevGate` **only when both** conditions hold
(`agents/jev.py:367-381`):

1. `jev_config().enabled` — i.e. `provider != "off"` (`settings.py:194-197`);
2. `<base_dir>/models/jev_calibration.json` exists and is readable.

Otherwise it returns `None` and the deterministic `BandGate`/`LearnedGate`
behaviour is unchanged.

`JevGate` mirrors `LearnedGate`'s guard (`agents/jev.py:298-364`):

- It asks Jev **only inside the medium confidence band** — classify
  `low <= confidence < high`, extract `low <= confidence < judge_band_high`
  (`agents/jev.py:334-337`).
- It **never overrides a hard `rule` decision** (`re_sort`, length cap, invalid
  schema): `base.source == "rule"` returns the band decision untouched
  (`agents/jev.py:331-332`).
- The chosen action is accepted when the Jev confidence (temperature-scaled by
  the calibration, `agents/jev.py:320-327`) is ≥ the accept threshold and the
  `noul` answer does not call for escalation; otherwise it maps to
  `human_review`. `noul >= 0.5` is the escalation cut (`_NOUL_ESCALATE`,
  `agents/jev.py:69`, `agents/jev.py:357-360`), and an unknown `choice` also
  maps to `human_review` (`agents/jev.py:361-362`). Decisions carry
  `source="jev"` (`agents/jev.py:343-364`; `source` literal at
  `agents/gate.py:53`).

## Configuration

The taxonomy block (`config/taxonomy.yaml:142-160`) and the environment
variables are two views of the same fields. Per-field resolution is
`MAILROOM_JEV_<FIELD>` → `JEV_<FIELD>` → taxonomy `jev:` → default
(`_jev_env`/`_jev_field`/`_jev_scalar`, `settings.py:200-226`;
`JevConfig` docstring `settings.py:175-197`).

| Taxonomy key | Env (`MAILROOM_JEV_*` / `JEV_*`) | Default | Source |
| --- | --- | --- | --- |
| `provider` | `PROVIDER` | `off` | `settings.py:254-258`, `taxonomy.yaml:144` |
| `model` | `MODEL` | per provider (above) | `settings.py:262`, `taxonomy.yaml:147` |
| `base_url` | `BASE_URL` | per provider (above) | `settings.py:263`, `taxonomy.yaml:151` |
| `temperature` | `TEMPERATURE` | `1.0` | `settings.py:167-172`, `settings.py:265`, `taxonomy.yaml:153` |
| `accept_threshold` | `ACCEPT_THRESHOLD` | `0.8` | `settings.py:167-172`, `settings.py:266-268`, `taxonomy.yaml:156` |
| `timeout_s` | `TIMEOUT_S` | `10.0` | `settings.py:167-172`, `settings.py:269`, `taxonomy.yaml:158` |
| `max_retries` | `MAX_RETRIES` | `2` | `settings.py:167-172`, `settings.py:270`, `taxonomy.yaml:160` |

Unknown providers collapse to `off` (`settings.py:257-258`). `jev_config()` is
deliberately **not cached** so env/CLI overrides take effect immediately
(`settings.py:246-252`); `load_taxonomy`/`get_settings` are still `lru_cache`d,
so a taxonomy edit needs a process restart.

`.env.example:22-35` documents the same surface, including `MAILROOM_JEV_*`
blanks and `JEV_API_KEY` / `TYPESAFE_API_KEY`.

## CLI

The `jev` command group is `cli.py:246-327`.

```bash
# Ask one question; prints {"answers": {...}}.
uv run mailroom jev decide \
  --state "the document text or prompt" \
  --type choice \
  --instructions "Choose the next routing action." \
  --criteria proceed="Accept and continue." \
  --criteria retry="Re-run the stage."

# For --type score, pass ordered labels instead of KEY=DESCRIPTION pairs:
uv run mailroom jev decide \
  --state "..." --type score --instructions "Rate the extraction." \
  --criteria-list "1,2,3,4,5"

# Fit temperature + thresholds from train-split JSONL:
uv run mailroom jev calibrate --rows rows.jsonl --out models/jev_calibration.json
```

- `decide` flags (`cli.py:264-303`): `--state` (required), `--type`
  (`choice|noul|score`, required), `--instructions` (required), `--criteria`
  (repeatable `KEY=DESCRIPTION`, for `choice`/`noul`), `--criteria-list`
  (comma-separated labels, for `score`). It echoes a JSON
  `{"answers": {...}}` envelope. **When Jev is off it prints
  `Jev is off (MAILROOM_JEV_PROVIDER=off)...` to stderr and exits non-zero**
  (`_jev_off`, `cli.py:254-261`; called at `cli.py:280-282`). An unknown
  `--type` also exits non-zero (`cli.py:296-298`).
- `calibrate` flags (`cli.py:306-327`): `--rows` (required; must exist, be a
  readable file), `--out` (default `models/jev_calibration.json`). It loads the
  JSONL rows and calls `fit_jev_calibration`, echoing the fitted dict.

## Calibration

Jev's confidence must be calibrated on **your** data before the gate trusts it.
`fit_jev_calibration(rows, out)` (`eval/jev_calibration.py:106-138`):

1. Reuses the leakage guard `_check_train` — every row must have
   `split == "train"`, else `ValueError` before anything is written
   (`eval/jev_calibration.py:115`; `eval/train_gate.py:47-55`). Missing
   `confidence`/`correct` keys raise `KeyError`.
2. Fits a **temperature** by binary NLL minimisation, reusing
   `train_gate._fit_temperature` (`eval/jev_calibration.py:124-125`;
   `eval/train_gate.py:99-108`).
3. Searches two operating points on the **calibrated** confidence by balanced
   accuracy (`_search_thresholds`, `eval/jev_calibration.py:90-103`):
   **`accept_threshold`** (above it, the chosen action is trusted) and
   **`verify_threshold`** (the lower edge of the uncertainty band,
   `verify <= accept`).
4. Records **ECE before/after** both scalings (`agents/gate.py:183-195`) and
   writes `<out>` (default `models/jev_calibration.json`).

Rows are plain dicts `{split, confidence, correct}` with `correct ∈ {0,1}`.
The artifact is a flat JSON object
`{temperature, accept_threshold, verify_threshold, ece_before, ece_after, n}`
(`eval/jev_calibration.py:129-136`), read back by `load_jev_calibration`
(`eval/jev_calibration.py:59-69`) and consumed by `load_jev_gate`. Empty input
writes a neutral calibration (`temperature 1.0`, `accept 0.8`, `verify 0.5`,
`ece 0.0`) (`_NEUTRAL`, `eval/jev_calibration.py:40-44`, `106-120`).

**Careful calibration — why it matters.**

- Jev's shipped calibration is fit on the author's **teacher / MASSIVE** data
  and **does not transfer**; re-fit on your own mailroom rows (issue #8).
- Official guidance is **confidence-gated routing**: high confidence → *act*,
  medium → *caution*, low → *human*. The code names are `accept_threshold`
  (act) and `verify_threshold` (the lower edge of the caution band)
  (`eval/jev_calibration.py:11-13`, `90-103`). Re-fit both thresholds on your
  own data.
- The gate currently consumes **only `accept_threshold`** (from the calibration
  when present, else `cfg.accept_threshold`) (`agents/jev.py:345-349`).
  `verify_threshold` is fit and persisted but **not read by `JevGate` yet** —
  the medium/caution band is still the deterministic one. Flagged so nobody
  assumes a two-threshold policy is live.
- Third-party evals measured ECE ~0.093 versus the shipped 0.03–0.06; re-fit
  ECE is reported in the artifact as `ece_before`/`ece_after` (issue #8).

## Caveats

- **Freeze the wording.** The option and criteria strings are read by the model,
  so the exact criteria strings used at calibration time must be frozen — any
  edit invalidates the calibration (issue #8).
- **Known weak spots.** Expect degradation on **dates and numbers** and on
  out-of-scope abstention (CLINC150 out-of-scope recall ~27%); a sealed
  JevBench v1.4 run scored ~33%, so treat public headlines as optimistic
  (issue #8).
- **No idempotency.** A request is not deduplicated; only HTTP `429` and `5xx`
  are retried (with exponential backoff honouring `Retry-After`), and any other
  non-2xx raises `JevError` immediately (`agents/jev.py:230-248`,
  `agents/jev.py:191-199`). Retrying a successful-but-unseen call can double a
  side effect, so keep callers idempotent.
- **Pin the model id.** Once thresholds are tuned against a dated model id,
  pin that exact id (`model:`) rather than a floating alias, so the calibration
  keeps matching the model (issue #8).

## Tests

Jev ships with fake-transport tests only — **no network** (`tests/agents/test_jev.py`,
378 lines; `tests/eval/test_jev_calibration.py`, 89 lines). They cover payload
shape, all three answer types, confidence derivation, the 429/529/5xx retry
policy, gate overrides/rule-preservation/band-scoping, the calibration accept
threshold, the leakage guard and the neutral empty calibration.

## Cross-links

- [CONFIGURATION.md](CONFIGURATION.md) — env vars and the `jev:` taxonomy block.
- [EVALUATION.md](EVALUATION.md) — Jev calibration in the fitting workflow.
- [ARCHITECTURE.md](ARCHITECTURE.md) — the gate slot Jev can back.
