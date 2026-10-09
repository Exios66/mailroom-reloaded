# Jev (TypeSafe System One)

Jev is an **opt-in, probabilistic decision model** — the TypeSafe *System One*
— that can back the pipeline's route gate (`agents/gate.py`). It is **not a
chat/generation LLM**: it returns typed, calibrated *answers* to structured
questions rather than free text. When Jev is off (the default) the pipeline is
byte-for-byte unchanged; when it is on **and calibrated**, `load_gate()` prefers
a `JevGate` over the deterministic `BandGate` / `LearnedGate`.

Source of record: tracking issue
[Exios66/mailroom-reloaded#8](https://github.com/Exios66/mailroom-reloaded/issues/8).
Implementation: `src/mailroom_reloaded/agents/jev.py` (`JevClient`, `JevGate`,
`load_jev_gate`), `src/mailroom_reloaded/eval/jev_calibration.py`
(`fit_jev_calibration`), the `jev` command group in `cli.py:249-331`, the `jev:`
block in `config/taxonomy.yaml:142-160`, and `settings.py` (`JevConfig`,
`jev_config`, `_jev_api_key`).

## What Jev is

`JevClient.ask` sends one envelope and parses an `answers` mapping (request
`{"model", "state", "questions"}`; response parsed by `_parse_answers`). Each
question is built by one of three builders and each answer is normalized into a
typed `JevAnswer` by `_as_answer`:

| Type | Shape | Meaning |
| --- | --- | --- |
| `choice` | `{type, choice, probabilities, confidence}` | One option plus the per-option probability distribution and a confidence. |
| `noul` | `{type, noul}` | A single `P(yes)` probability (built by `noul`). |
| `score` | `{type, score, legend}` | A probability-weighted position with a label legend (built by `score`). |

`confidence` is **derived from the distribution concentration, not from
per-item correctness**: when the server omits it, the client falls back to the
probability of the chosen option, else the maximum probability (in
`_as_answer`). It is not a separately calibrated decision head — the
issue flags that "confidence is the top option probability, not a separately
calibrated decision head". Treat it as a raw signal that the calibration
(§ Calibration) must re-scale before the gate trusts it.

The question builders are:

- `choice(name, instructions, criteria)` — `criteria` maps **each option → a
  description**.
- `noul(name, instructions, criteria=None)` — missing criteria normalizes to
  `{}`.
- `score(name, instructions, criteria)` — `criteria` is an ordered list of
  score labels.

The gate asks exactly two questions: a `route` **choice** over
`proceed | retry | verify | boss | human_review`, and an `escalate` **noul**
"should this be escalated to a human reviewer?" (`_jev_questions`).

## Transports

`MAILROOM_JEV_PROVIDER` selects one of three transports (`_JEV_PROVIDERS`,
`_JEV_PROVIDER_DEFAULTS`, `jev_config` in `settings.py`). All three use
the same typed-decision HTTP shape; only the endpoint and default model differ.

| Provider | Endpoint | Default model | API key | Context |
| --- | --- | --- | --- | --- |
| `openrouter` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` | `OPENROUTER_API_KEY` | 32K (issue #8) |
| `typesafe` | `POST https://api.typesafe.ai/v1/systemone` | `jev-latest` | `TYPESAFE_API_KEY` | 64K — **not confirmed in code (see below)** |
| `local` | `POST http://127.0.0.1:8090/v1/systemone` | `jevk5` | optional | self-hosted JevK5 |

- **OpenRouter Decisions API** (`_JEV_PROVIDER_DEFAULTS` in `settings.py`). Not OpenAI-compatible;
  the issue notes "input `$0.042/M`, output free; 32K ctx".
- **TypeSafe native** (`_JEV_PROVIDER_DEFAULTS` in `settings.py`). The 64K context figure appears
  only in this documentation's brief, not in the code or issue — flagged
  unconfirmed.
- **Local / offline `jevk5`** (`_JEV_PROVIDER_DEFAULTS` in `settings.py`). A self-hosted JevK5
  server with the same typed-decision shape and one forward pass. The JevK5
  runtime internally reads letter logits as `softmax(logit / 1.22)` per the
  model card in issue #8; that 1.22 is the runtime's own softmax divisor, **not**
  the request `temperature` mailroom sends. `temperature` is a local-usage/default
  knob reserved for the offline JevK5 transport (`_JEV_DEFAULTS`, default `1.0`,
  set via `MAILROOM_JEV_TEMPERATURE`/`JEV_TEMPERATURE`); it is **not** included
  in hosted OpenRouter/TypeSafe requests. `1.22` is not present anywhere in this
  repository — flagged as issue-sourced.

Requests carry `Authorization: Bearer <api_key>` only when a key resolves; the
local provider may run keyless (`JevClient.ask`; `_jev_api_key`).

**Key resolution order** (`_jev_api_key`, `src/mailroom_reloaded/settings.py`;
mirrored in `.env.example:32-39`):

1. `MAILROOM_JEV_API_KEY` / `JEV_API_KEY` — explicit override (via
   `_jev_env("API_KEY")`), tried first for every provider.
2. Provider-preferred hosted key: `OPENROUTER_API_KEY` when
   `provider=openrouter`; `TYPESAFE_API_KEY` when `provider=typesafe`.
3. The other hosted key (`TYPESAFE_API_KEY` for `openrouter`,
   `OPENROUTER_API_KEY` for `typesafe`).
4. `settings.openrouter_api_key` (`OPENROUTER_API_KEY` /
   `MAILROOM_OPENROUTER_API_KEY` as loaded by pydantic-settings).

## How it plugs in

Routing is an opt-in swap in `load_gate` (`agents/gate.py:169-180`):

```python
jev = load_jev_gate(load_taxonomy())
if jev is not None:
    return jev
# else: LearnedGate if models/route_gate.json exists, else BandGate
```

`load_jev_gate` returns a `JevGate` **only when both** conditions hold:

1. `jev_config().enabled` — i.e. `provider != "off"` (`JevConfig.enabled`);
2. `<base_dir>/models/jev_calibration.json` exists and is readable.

Otherwise it returns `None` and the deterministic `BandGate`/`LearnedGate`
behaviour is unchanged.

`JevGate` mirrors `LearnedGate`'s guard (`JevGate.decide`):

- It asks Jev **only inside the medium confidence band** — classify
  `low <= confidence < high`, extract `low <= confidence < judge_band_high`
  (`JevGate.decide`).
- It **never overrides a hard `rule` decision** (`re_sort`, length cap, invalid
  schema): `base.source == "rule"` returns the band decision untouched
  (`JevGate.decide`).
- The chosen action is accepted when the Jev confidence (temperature-scaled by
  the calibration, `JevGate._confidence`) is at/above the accept threshold and
  the `noul` answer does not call for escalation. Between the verify and accept
  thresholds the gate returns `verify` (or `human_review` for a non-`proceed`/
  `verify` choice); below the verify threshold it returns `human_review`
  (`JevGate.decide`). `noul >= 0.5` is the escalation cut (`_NOUL_ESCALATE`),
  and an unknown `choice` also maps to `human_review`. Decisions carry
  `source="jev"` (`JevGate.decide`; `source` literal at `agents/gate.py:53`).

## Configuration

The taxonomy block (`config/taxonomy.yaml:142-160`) and the environment
variables are two views of the same fields. Per-field resolution is
`MAILROOM_JEV_<FIELD>` → `JEV_<FIELD>` → taxonomy `jev:` → default
(`_jev_env`/`_jev_field`/`_jev_scalar`; `JevConfig` docstring).

| Taxonomy key | Env (`MAILROOM_JEV_*` / `JEV_*`) | Default | Source |
| --- | --- | --- | --- |
| `provider` | `PROVIDER` | `off` | `jev_config`, `taxonomy.yaml:144` |
| `model` | `MODEL` | per provider (above) | `jev_config`, `taxonomy.yaml:147` |
| `base_url` | `BASE_URL` | per provider (above) | `jev_config`, `taxonomy.yaml:151` |
| `temperature` | `TEMPERATURE` | `1.0` | `jev_config`, `taxonomy.yaml:153` |
| `accept_threshold` | `ACCEPT_THRESHOLD` | `0.8` | `jev_config`, `taxonomy.yaml:156` |
| `timeout_s` | `TIMEOUT_S` | `10.0` | `jev_config`, `taxonomy.yaml:158` |
| `max_retries` | `MAX_RETRIES` | `2` | `jev_config`, `taxonomy.yaml:160` |

Unknown providers collapse to `off` (`jev_config`). `jev_config()` is
deliberately **not cached** so env/CLI overrides take effect immediately;
`load_taxonomy`/`get_settings` are still `lru_cache`d, so a taxonomy edit needs
a process restart.

`.env.example:22-39` documents the same surface, including `MAILROOM_JEV_*`
blanks and `MAILROOM_JEV_API_KEY` / `JEV_API_KEY` / `TYPESAFE_API_KEY`.

## CLI

The `jev` command group is `cli.py:249-331`.

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

- `decide` flags (`cli.py:268-306`): `--state` (required), `--type`
  (`choice|noul|score`, required), `--instructions` (required), `--criteria`
  (repeatable `KEY=DESCRIPTION`, for `choice`/`noul`), `--criteria-list`
  (comma-separated labels, for `score`). It echoes a JSON
  `{"answers": {...}}` envelope. **When Jev is off it prints
  `Jev is off (MAILROOM_JEV_PROVIDER=off)...` to stderr and exits non-zero**
  (`_jev_off`, `cli.py:257-264`; called at `cli.py:283-285`). An unknown
  `--type` also exits non-zero (`cli.py:299-301`).
- `calibrate` flags (`cli.py:309-330`): `--rows` (required; must exist, be a
  readable file), `--out` (default `models/jev_calibration.json`). It loads the
  JSONL rows and calls `fit_jev_calibration`, echoing the fitted dict.

## Calibration

Jev's confidence must be calibrated on **your** data before the gate trusts it.
`fit_jev_calibration(rows, out)` (`eval/jev_calibration.py:162-200`):

1. Reuses the leakage guard `_check_train` — every row must have
   `split == "train"`, else `ValueError` before anything is written
   (`eval/jev_calibration.py:173`; `eval/train_gate.py:47-55`). Missing
   `confidence`/`correct` keys raise `KeyError`.
2. Fits a **temperature** by binary NLL minimisation, reusing
   `train_gate._fit_temperature` (`eval/jev_calibration.py:186-188`;
   `eval/train_gate.py:99-108`).
3. Searches two operating points on the **calibrated** confidence by balanced
   accuracy (`_search_thresholds`, `eval/jev_calibration.py:115-139`):
   **`accept_threshold`** (above it, the chosen action is trusted) and
   **`verify_threshold`** (the lower edge of the uncertainty band,
   `verify <= accept`).
4. Records **ECE before/after** both scalings (`agents/gate.py:183-195`) and
   writes `<out>` (default `models/jev_calibration.json`).

Rows are plain dicts `{split, confidence, correct}` with `correct ∈ {0,1}`.
The artifact is a flat JSON object
`{temperature, accept_threshold, verify_threshold, ece_before, ece_after, n}`
(`eval/jev_calibration.py:190-198`), read back by `load_jev_calibration`
(`eval/jev_calibration.py:77-94`) and consumed by `load_jev_gate`. Empty input
writes a neutral calibration (`temperature 1.0`, `accept 0.8`, `verify 0.5`,
`ece 0.0`) (`_NEUTRAL`, `eval/jev_calibration.py:41-45`, `174-179`).

**Careful calibration — why it matters.**

- Jev's shipped calibration is fit on the author's **teacher / MASSIVE** data
  and **does not transfer**; re-fit on your own mailroom rows (issue #8).
- Official guidance is **confidence-gated routing**: high confidence → *act*,
  medium → *caution*, low → *human*. The code names are `accept_threshold`
  (act) and `verify_threshold` (the lower edge of the caution band)
  (`eval/jev_calibration.py:11-13`, `115-139`). Re-fit both thresholds on your
  own data.
- **No-separation guard (issue #14).** When the best balanced-accuracy plateau
  is no better than chance, `_search_thresholds` returns the neutral operating
  points (`accept 0.8` / `verify 0.5`) instead of the plateau edges. Without
  this, a degenerate label source (all-`false` `retry_expected`/`review_expected`)
  produced `accept=1.0` / `verify=0.0`: route confidence below 1.0 enters the
  verify band, where choices can escalate to human review; only confidence
  1.0 reaches accept. The `fixtures` config in
  `Lucius-Morningstar/mailroom-reloaded-fixtures` is the positive-label source;
  `scripts/jev_harvest.py --mode features` also refuses a single-class batch.
- The gate consumes **both thresholds**. Inside the medium band Jev's
  temperature-scaled confidence is tiered (`JevGate.decide` in
  `src/mailroom_reloaded/agents/jev.py`): `confidence < verify_threshold` →
  `human_review`; `verify_threshold <= confidence < accept_threshold` →
  `verify` (a Jev escalation choice such as `boss`/`human_review` is never
  downgraded to `verify` — it stays `human_review`); `confidence >=
  accept_threshold` → the chosen action. **Without a calibration there is no
  verify band**, because `verify_threshold` collapses to `accept_threshold`.
  Covered by `test_jev_gate_uses_calibration_accept_threshold`,
  `test_jev_gate_below_verify_threshold_is_human_review` and
  `test_jev_gate_medium_band_keeps_escalation_choice` in
  `tests/agents/test_jev.py`.
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
  non-2xx raises `JevError` immediately (`JevClient._post_with_retry`,
  `_retry_after`). Retrying a successful-but-unseen call can double a
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

## Local dev with Jev

`JEV=1 scripts/tui_dev.sh up` wires the opt-in gate end-to-end with no network:

- `deploy/mock_jev.py` (`127.0.0.1:8898`, `POST /v1/systemone`, `GET /health`)
  answers the `route` choice and `escalate` noul questions deterministically
  from the gate features (confidence >= 0.90 proceed, 0.87-0.90 verify, lower
  escalates), or from a `[jev:proceed|verify|boss|...]` marker in a string state.
- `scripts/jev_dev_rows.py` emits 60 synthetic `split=train` rows;
  `mailroom jev calibrate` writes `data/tui-dev/base/models/jev_calibration.json`.
- The API runs with `MAILROOM_JEV_PROVIDER=local` and
  `MAILROOM_JEV_BASE_URL=http://127.0.0.1:8898/v1/systemone`.
- `deploy/mock_openai.py` honours a `[confidence:0.NN]` marker in the document
  text, so seeded docs (`scripts/tui_seed_jev/*.txt`) land in the correspondence
  classify medium band (0.85 <= c < 0.95), where the gate consults Jev.
- Observe it: `GET /v1/jev` (provider, calibration, active `gate`; never keys)
  and `GET /v1/audit/{doc_id}`, whose `gate_classify` / `gate_extract`
  `gate_decision` entries carry `source` (`band`, `rule`, `model` or `jev`).

Caveat: dev correspondence extraction confidence is `0.6*coverage + 0.4`, which
never lands in the extract medium band, so only the classify gate consults Jev
locally. Classify has no `verify` route, so a Jev `verify` parks the document.
Without `JEV=1` the stack is unchanged and `GET /v1/jev` reports `gate: band`.
