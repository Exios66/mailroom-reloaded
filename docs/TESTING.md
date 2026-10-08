# Testing

`mailroom-reloaded` uses `pytest` (with `pytest-asyncio` in auto mode) and
`ruff`. This page covers the test tiers, the `live` marker, the dependency
fence, and how to run the suites. The day-to-day dev-server suite is owned by
the dev-server tooling — see [DEV_SERVER.md](DEV_SERVER.md) and
[scripts/dev.sh](../scripts/dev.sh).

## Default suite

```bash
uv sync --extra dev
uv run pytest -q
```

`pyproject.toml:61-67` sets `testpaths = ["tests"]`, `asyncio_mode = "auto"`,
`addopts = "-m 'not live'"`, and registers the `live` marker. So the default run
**deselects every `@pytest.mark.live` test**; you do not need `-m "not live"`
yourself. The scoring-parity test is additionally skipped unless the pinned
upstream package is installed (`tests/scoring/test_parity.py:7` uses
`pytest.importorskip("llm_dojo_scoring")`, provided by the `parity` extra,
`pyproject.toml:49`).

## Test tiers

Tests are grouped by module under `tests/`:

| Tier | Directory | Covers |
| --- | --- | --- |
| Agents | `tests/agents/` | sorter, specialists, gate, CrewAI judge/arbiter/boss |
| Pipeline | `tests/pipeline/` | flow routes, guards/state, report + archive |
| Eval | `tests/eval/` | dataset loader, runner, metrics/cards, train-gate |
| Observability | `tests/obs/` | spans, GenAI attrs, masking, metric names |
| API | `tests/api/` | TestClient `/v1` routes, auth, UI |
| Deploy | `tests/deploy/` | compose config, collector config, Modal config |
| Intake | `tests/intake/` | Gmail attachment decoding/ingest |
| Ingest | `tests/ingest/` | clerk/pdf/vision, BERT adapter |
| LLM | `tests/llm/` | client resolve/structured calls, tooling, retry |
| Scoring | `tests/scoring/` | vendored dojo subset + upstream parity |
| Schemas/Storage | `tests/schemas/`, `tests/storage/` | extraction schemas, audit chain, bins |
| Top-level | `tests/test_*.py` | dependency fence, prompt lock, settings, tools, watcher/review |

Fixtures and helpers: `tests/fakes/openai_server.py` (scripted OpenAI-compatible
fake), `tests/helpers.py` (`assert_no_gt` ground-truth leak check), and
`tests/eval/fixtures/mini_dataset/` (offline blind + ground-truth JSONL).
`tests/conftest.py` clears the settings/taxonomy `lru_cache` around every test
(`tests/conftest.py:6-12`).

Run a tier directly, e.g.:

```bash
uv run pytest tests/pipeline -v
uv run pytest tests/eval -v
uv run pytest tests/api -v
```

## Live tests

Tests that call a real provider are marked `@pytest.mark.live` and additionally
guard on `MAILROOM_LIVE=1` (`tests/llm/test_client.py:212-213`,
`tests/agents/test_crew_agents.py:176-179`). The marker is registered in
`tests/agents/conftest.py:5-6` and `tests/llm/conftest.py:5-6`. Run them
explicitly after configuring a provider in `.env`:

```bash
MAILROOM_LIVE=1 uv run pytest -m live -v
```

Because `addopts` deselects `live` by default, you must pass `-m live` (or
`-m "live or not live"`) to include them. A live run also needs the environment
its chosen test expects (`DEFAULT_PROVIDER` plus credentials); see
[CONFIGURATION.md](CONFIGURATION.md).

## Dependency fence

`tests/test_dependency_fence.py` enforces the design's library budget: no
`langgraph`, `langchain*`, `langfuse`, `braintrust` or `litellm` imports anywhere
under `src/`, and none of them in any dependency or extra
(`tests/test_dependency_fence.py:7`, `tests/test_dependency_fence.py:10-30`). Run
it in isolation with `uv run pytest tests/test_dependency_fence.py -q`.

## Lint

```bash
uv run ruff check .
```

Ruff is in the `dev` extra (`pyproject.toml:48`). The vendored scoring modules
are excluded from lint and must keep upstream bytes (`pyproject.toml:69-79`;
see `src/mailroom_reloaded/scoring/PARITY.md`).

## Prompt lock

`tests/test_prompts.py` verifies the sha256 of every vendored prompt against its
`lineage.json`, so the frozen v1 / SAND-37 prompt bytes cannot drift silently
(spec §11; `tests/test_prompts.py`).

## Scoring parity

When the pinned `llm-dojo-scoring` is installed
(`uv sync --extra parity`), `tests/scoring/test_parity.py` asserts the vendored
`scoring/` subset reproduces upstream `score_extraction` on ten fixture pairs
(`tests/scoring/test_parity.py:7`, `tests/scoring/test_parity.py:15-25`).
Without the extra, the whole file is skipped.

## Dev-server suite

The dev-server tooling wraps dependency sync, lint and tests behind one entry
point (owned by that tooling; see [DEV_SERVER.md](DEV_SERVER.md)):

```bash
scripts/dev_test.sh             # uv sync --extra dev; ruff check; pytest (live deselected)
scripts/dev_test.sh --live      # also run `live` tests (needs scripts/dev.sh up)
scripts/dev_test.sh --no-lint   # skip ruff
make dev-test                   # thin wrapper for scripts/dev_test.sh
```

`scripts/dev_test.sh` is the only entry point that installs dependencies.
`make test`, `make lint` and `make smoke` wrap `uv run pytest`,
`uv run ruff check .` and `scripts/dev.sh smoke` respectively
(`Makefile`; `docs/DEV_SERVER.md`). [README](../README.md#docker) has the
production compose profiles.

## Cross-links

- [ARCHITECTURE.md](ARCHITECTURE.md) — what the pipeline tests exercise.
- [EVALUATION.md](EVALUATION.md) — `tests/eval` and the train/test contract.
- [OPERATIONS.md](OPERATIONS.md) — the smoke test and stack health.
- [DEV_SERVER.md](DEV_SERVER.md) — the local dev workflow.
