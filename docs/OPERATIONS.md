# Operations

How to run, watch, review, verify and observe a `mailroom-reloaded` deployment.
For config values see [CONFIGURATION.md](CONFIGURATION.md); for the pipeline
internals see [ARCHITECTURE.md](ARCHITECTURE.md); for the local dev stack see
[DEV_SERVER.md](DEV_SERVER.md).

## Watcher

The watcher drains `inbox/` into the pipeline, one atomic claim per file.

```bash
uv run mailroom watch --worker-id prod-1 --concurrency 4   # standalone
uv run mailroom serve --watch                              # API + embedded watcher
```

- `Watcher.run_forever` holds an exclusive `flock` on `<base_dir>/watcher.lock`;
  a second draining process raises `WatcherLockHeld`. **Never run the embedded
  watcher and the standalone `watcher` service against the same data dir**
  (`watcher.py:39-56`, `watcher.py:194-224`).
- `drain_once` lists processable inbox files (skips dotfiles and `*.meta`),
  records `queue_depth`, and processes them serially or through a bounded
  `ThreadPoolExecutor` (`watcher.py:48-53`, `watcher.py:163-191`). `--concurrency`
  is clamped to 1–32 (`cli.py` `watch`, `watcher.py:114`).
- `watchdog` observers wake the poller on create/modify/move; a 1 s poll is the
  fallback (`watcher.py:40`, `watcher.py:226-258`).
- On startup `resume_processing` re-runs every manifest still in status
  `processing` from its next unfinished node; audit entries are deduped so the
  chain gains no duplicates (`watcher.py:121-149`, `storage/audit_log.py:41-45`).

Deployment shapes:

- **Embedded** (`MAILROOM_EMBED_WATCHER=1`) — the API lifespan starts a daemon
  watcher thread (`api/app.py:349-376`). This is the compose default.
- **Split** (`split-watcher` profile) — a dedicated `mailroom watch` container;
  run `app` with `MAILROOM_EMBED_WATCHER=0` (`docker-compose.yml:43-57`).

## Human review

A document routed to `human_review` is moved to `review/` with manifest status
`parked` (`pipeline/flow.py:365-383`). An operator dispositions it through the
API or the library; **there is no review CLI command**.

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/review/<doc_id>/resolve" \
  -H "Authorization: Bearer $MAILROOM_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"correct","doc_type":"contract","doc_subclass":"license","reviewer":"alice"}'
```

| Action | Effect | Source |
| --- | --- | --- |
| `approve` | Re-runs from `extract` with the existing classification (no sorter call). | `review.py:80-101` |
| `correct` | Re-runs from `extract` with a corrected `doc_type` / `doc_subclass` override. | `review.py:61-66`, `review.py:80-101` |
| `reject` | Moves the file to `failed/` and sets status `failed`. | `review.py:68-78` |

Every disposition appends a hash-chained `review_resolved` audit entry
(`review.py:76`, `review.py:93`). The endpoint returns 404 when no parked
manifest matches (`api/app.py:230-233`). The source path is recovered from the
manifest state or the `review/` bin filename (`review.py:104-119`).

## Audit verification

```bash
curl -sS "http://127.0.0.1:8000/v1/audit/<doc_id>" \
  -H "Authorization: Bearer $MAILROOM_API_TOKEN"
```

Returns the entry list plus `{ok, broken_at}` from `verify_chain`
(`api/app.py:208-217`). Each entry chains `prev_hash` → `entry_hash` (sha256 of
the canonical entry) and is keyed `(doc_id, seq)` (`schemas/audit.py:44-49`,
`storage/db.py:25-36`). The chain detects edits, middle deletions and reordering;
**tail truncation cannot be detected without an external anchor** — export or
externally anchor the SQLite DB if that threat is in scope
(`storage/audit_log.py:44-45`). The archive ledger has its own anchor, see
[Ledger anchor](#ledger-anchor).

The SQLite DB is `<base_dir>/mailroom.db`, WAL mode, `busy_timeout=5000`, and is
created lazily (`storage/db.py:56-73`). Back it up together with `manifests/`,
`archive/` and `review/` for a complete restore.

## Ledger anchor

The archive ledger is one hash chain over every run. A chain on one disk detects edits but
not truncation or a restored backup, so the head can be anchored off-host. Configuration:
[CONFIGURATION.md](CONFIGURATION.md#archive-ledger-anchor). The remote table, role and grants
are in `deploy/anchor/mailroom_anchor.sql`.

```bash
mailroom audit verify [--run ID] [--external]
mailroom audit anchor
mailroom audit export-head [--out PATH]
```

- `audit verify` checks the local chain (one run with `--run`). Without `--external` it
  exits only 0 or 1. With `--external` it also compares the head with the anchor.
- `audit anchor` pushes the current head now. Pushes also happen automatically in a
  background thread after `run_closed`, pin, unpin, policy and prune entries; they make up to
  three attempts and never block or fail the pipeline.
- `audit export-head` prints `seq`, `entry_hash`, `ts` and `exported_at` as JSON (or writes
  it to `--out/-o`), whatever `MAILROOM_ANCHOR` is set to. An empty ledger prints
  `ledger empty` and exits 0. Pin the output somewhere the pipeline host cannot write.

| Exit | Meaning |
| --- | --- |
| 0 | OK. A fresh unanchored tail is still 0. |
| 1 | Tamper: chain broken, or (`--external`, `anchor`) the anchor conflicts with the ledger (truncated or rewritten). |
| 3 | Anchor store unreachable. |
| 4 | Anchor not configured (`none`, unknown value, missing URL/key, or `export`, which has no remote). |
| 5 | STALE: the oldest unanchored entry is older than 24 h. |

Exit 2 is reserved for command-line usage errors.

### What the anchor protects

It protects against truncation, rollback (including a restored backup) and rewrite of entries
at or before the last anchor pushed, by an attacker who does not hold the writer credential.

It does not protect:

- the unanchored tail (entries after the last push);
- runs that are still open;
- the correctness of what was recorded (it proves integrity, not truth);
- the validity of the chain at push time: a push anchors the current head without
  re-verifying earlier entries, so run `mailroom audit verify` before trusting an anchor;
- against a holder of the writer credential, who can append anchors over a rewritten tail.
  An off-host `export-head` copy taken earlier mitigates this.

The Supabase grants, the trigger and the PostgREST key header in the SQL file are unverified
until tried on a staging project. Test there before relying on the anchor.

## Replay and ledger views

The `/tui` terminal reads the archive ledger and replays runs; both are read-only except
pin, unpin and `keep set`, which append ledger entries.

| Command | API | Use |
| --- | --- | --- |
| `ledger [--run ID] [--kind K] [--limit N]` | `GET /v1/ledger` | Entries, newest first. |
| `ledger head` | `GET /v1/ledger/head` | Head seq, hash and entry count. |
| `ledger verify [run_id]` | `GET /v1/ledger/verify` | Re-checks the hash chain (and a closed run's Merkle root). Not cached; each call walks the chain. |
| `runs pin\|unpin <run_id>` | `POST /v1/ledger/pin\|unpin` | Protect a run's spans from pruning (or release it). |
| `runs keep` / `runs keep set <pinned\|all\|recent:N>` | `GET /v1/ledger/keep` / `POST /v1/ledger/policy` | Show or change the retention policy. |
| `replay [<run_id>]` | `GET /v1/replay/sessions[/{id}/timeline]` | List sessions or open the viewer. |

A run whose spans retention removed lists as `data pruned`; opening it answers 410 and
`ledger --run <id>` still shows its entries. Deep links: `/tui#replay=run:<id>` opens the viewer
after boot, and the `/ui` runs table links each run there. The fragment is never sent to the
server and only ids the `replay` command accepts are acted on. When `API_TOKEN` is set these routes
are token-gated like the rest of `/v1`. `/tui` keeps its token in its own tab's session storage,
so arriving from `/ui` needs `auth <token>` once; the link is then run again by hand
(`replay run:<id>`).

## Observability

The app emits OpenTelemetry traces and metrics; the compose stack ships a
collector, Phoenix, Prometheus and Grafana.

| Surface | URL (prod compose) | Contents |
| --- | --- | --- |
| API + `/ui` | http://localhost:8000 | documents, review queue, audit verify, runs/cards |
| Phoenix | http://localhost:6006 | traces (OpenInference CrewAI + OpenAI spans) |
| Prometheus | http://localhost:9090 | metrics store |
| Grafana | http://localhost:3000 | provisioned dashboards |

`mailroom-reloaded` auto-runs `setup_tracing()` on import
(`__init__.py:7-9`). Spans: `mailroom.document` and `mailroom.node.<node>`
(`pipeline/flow.py:300`, `pipeline/flow.py:568`); the OpenInference instrumentors
wrap CrewAI and OpenAI (`obs/tracing.py:167-180`). Metrics are the twelve
instruments in `obs/metrics.py:28-41`, including `mailroom.documents`,
`mailroom.node.duration`, `mailroom.llm.calls`, `mailroom.gate.decisions`,
`mailroom.bert.route`, `mailroom.schema.valid`, `mailroom.length_capped`,
`mailroom.cost.usd`, `mailroom.queue.depth`, `mailroom.inflight`, and the GenAI
semconv `gen_ai.client.token.usage` / `gen_ai.client.operation.duration`.

- **Phoenix project** is the `openinference.project.name` resource attribute:
  `MAILROOM_PHOENIX_PROJECT` (default `mailroom-live`, `obs/tracing.py:133-135`).
- **Trace masking.** Set `MAILROOM_TRACE_MASK=1` to replace prompt/completion
  content with `<masked>` before export (`obs/tracing.py:67-99`).
- **Collector.** OTLP in on 4317/4318, traces → Phoenix, metrics → Prometheus
  `:8889`; it also scrapes vLLM `/metrics`, DCGM and docker_stats
  (`deploy/otel-collector.yaml:1-62`). Prometheus scrapes `otel-collector:8889`.
- **Grafana dashboards** (provisioned): `Pipeline`, `Serving & GPU`, `Quality`
  (`deploy/grafana/dashboards/`).
- **`run_id` on every metric.** Each data point carries `run_id` and `environment`
  (`obs/metrics.py`): an eval run is one `run_id`, live traffic is one daily bucket
  `live-<YYYYMMDD>` (or `MAILROOM_RUN_ID`). `doc_id` is never a label. Pick one run in
  the `run_id` variable and the `replay ↗` dashboard link (and the per-run table's row
  links) open it in the `/tui` viewer; `phoenix ↗` opens Phoenix, where spans filter on
  `mailroom.run_id`. The links' base URLs are the dashboards' hidden constants
  `public_url` (default `http://localhost:8000`) and `phoenix_url` (default
  `http://localhost:6006`); change them in the dashboard JSON for a non-local deploy.
- **Decision counters.** `mailroom.retries{kind}`, `mailroom.escalations{to}` and
  `mailroom.review.causes{cause}` feed the Pipeline dashboard's *Decisions* row, the
  same retry / escalation / review-cause mix the replay's event ticker shows.
- **GPU profile** adds local vLLM (`:8001`) and `dcgm-exporter` (`:9400`);
  Modal / remote vLLM `/metrics` is scraped through `VLLM_METRICS_URLS`
  (`docker-compose.yml:81-102`, `deploy/README.md:34`).

Observability and engine ports bind to `127.0.0.1` only in the prod compose
(`docker-compose.yml:68-69`, `docker-compose.yml:107`, etc.).

## Cost

- Per-document estimated cost is attached to each report (`cost.usd`,
  `pricing: per_token_estimate`) from `taxonomy.cost_models` using the sorter's
  configured model price (`pipeline/report.py:54-71`). Unknown models estimate
  `0.0`.
- Per-cell eval cost is GPU-hour (`wall × GPUs × --gpu-usd-per-hour`) or
  per-token; see [EVALUATION.md](EVALUATION.md) and `eval/cost.py:70-120`.
- Live LLM cost is also emitted as the `mailroom.cost.usd` OTel counter per call
  (`llm/client.py:201-226`, `obs/metrics.py:36`).

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Ingest error / unsupported / empty document | `ingest` calls `_fail_node` with `ingest_failed:<error>`; file moves to `failed/`, manifest `failed`, audit `node_failed` (`ingest/clerk.py:139-141`, `pipeline/flow.py:341-363`). |
| Output hits the length cap | `LengthFinishReasonError`; the merge dagger mode re-samples once, otherwise `error_kind=LengthFinishReasonError` and the gate counts it as length-capped (`agents/specialists.py:185-222`, `pipeline/flow.py:415-417`). |
| Malformed JSON | One repair re-ask, then `parse_error` and retry/human-review per the gate (`agents/specialists.py:200-221`). |
| Node wall-clock/token budget exceeded | `_fail_node("deadline_exceeded"\|"token_budget_exceeded")` → `failed/` + audit (`pipeline/guards.py:29-42`, `pipeline/flow.py:311-316`). Deadlines are cooperative: checked after the node returns. |
| BERT missing / error / oversize / flag off | Fail-open to a `FULL` sort (`ingest/bert.py:44-111`, `ingest/bert.py:138-149`). |
| Transient endpoint errors (429, 5xx, timeout, cold 503) | `llm/retry.py` exponential backoff; Modal cold start backs off in minutes; transport retries do **not** consume the confidence retry budget (`llm/retry.py:1-9`, `llm/retry.py:58-106`). |
| Upload too large / unsupported type | HTTP 413 / 400 from `POST /v1/documents` (`api/app.py:142-154`). |
| Watcher crash mid-document | `resume_processing` restarts from the manifest's last completed node (`watcher.py:121-149`). |
| Catalog write fails | Logged and ignored — the catalog is best-effort; the manifest and archive remain authoritative (`pipeline/flow.py:255-256`). |
| Eval document raises | Recorded as an error row; the run continues (`eval/runner.py:404-411`). |

The `failed/` bin is the terminal sink for rejected and error documents
(`storage/bins.py:48-50`; `review.py:68-78`). Inspect the manifest
`manifests/<doc_id>.json` and `GET /v1/audit/<doc_id>` for the reason.

## Common runs

```bash
# Health and liveness
curl -sS http://127.0.0.1:8000/health

# Upload a document (multipart) and drain
curl -sS -X POST http://127.0.0.1:8000/v1/documents \
  -H "Authorization: Bearer $MAILROOM_API_TOKEN" -F file=@letter.txt

# One document through the pipeline directly
uv run mailroom run tests/ingest/fixtures/letter.txt

# Compose smoke test (needs the stack up)
scripts/smoke.sh        # prints SMOKE OK
```

Stack lifecycle is `docker compose -f deploy/docker-compose.yml ...` (see the
[README](../README.md#docker) profiles table) and, for day-to-day development,
`scripts/dev.sh up|down|logs|ps|reset|status|smoke`
(`scripts/dev.sh:1-168`) plus [DEV_SERVER.md](DEV_SERVER.md).

## Cross-links

- [ARCHITECTURE.md](ARCHITECTURE.md) — bins/manifest and audit design.
- [CONFIGURATION.md](CONFIGURATION.md) — every env var referenced above.
- [EVALUATION.md](EVALUATION.md) — cost and cards.
- [gmail-intake.md](gmail-intake.md) — the second intake door.
- [deploy/README.md](../deploy/README.md) — Modal vLLM deploy and teardown.
