# Docker verification, 2026-10-10 (issues #81, #82, #83, #84; plan R-04)

Host: macOS (Darwin 24.3.0), Docker Desktop 4.88.1, engine 29.7.2, Compose v2; `ghcr.io` and Docker Hub reachable.
Branch `claude/docker-verification` off `main` @ `4ad2df6`. Throwaway values only (`MAILROOM_API_TOKEN=x`,
`GRAFANA_ADMIN_PASSWORD=g`). Jobs were run one at a time and every image, container, volume and the build cache
was removed after each (`docker system df` back to the two base images, 299 MB).

Not run, by owner directive (keep builds small): the ModernBERT default app build (#81 step 7), the `local-llm`
llamafile build and the `gpu` profile (no NVIDIA host); those profiles are covered by `docker compose config -q` only.

## #81 app image (`deploy/Dockerfile`, lean)

| Step | Command | Result |
| --- | --- | --- |
| 1 | `docker build -f deploy/Dockerfile --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS= -t mrl-verify:lean .` | exit 0, 88 s, 1.59 GB |
| 2 | `docker run --rm mrl-verify:lean python -c "import mailroom_reloaded; print(mailroom_reloaded.__file__)"` | `/opt/venv/lib/python3.11/site-packages/mailroom_reloaded/__init__.py`; `mailroom --help` exit 0 |
| 3 | packaged schemas via `importlib.resources` | `content_files.json, event_kinds.v1.json, gen_spec.v1.json, overlay.v1.json, persona_behavior.v1.json, registry.v1.json, relation_kinds.v1.json, scenario.v2.json, signal_kinds.v1.json` |
| 4 | `id`; `touch /data/x` on a fresh named volume | `uid=10001(mailroom)`; file owned by 10001 |
| 5 | `docker run -d -p 127.0.0.1:18000:8000 -e MAILROOM_API_TOKEN=x mrl-verify:lean` | `/health` -> `{"status":"ok","service":"mailroom"}`; `State.Health.Status` = `healthy` |
| 6 | `docker run --rm mrl-verify:lean` (no token) | exit 1, `Refusing to bind to '0.0.0.0' without MAILROOM_API_TOKEN` |
| 6 | `--entrypoint python ... -m uvicorn mailroom_reloaded.api.app:app --host 0.0.0.0` (no token) | exit 3, same refusal |
| 6 | same with `MAILROOM_ALLOW_UNAUTHENTICATED_BIND=1` | starts: `Uvicorn running on http://0.0.0.0:8000` |
| 7 | ModernBERT build | skipped (owner directive: small builds only) |
| 8 | `docker build -f deploy/Dockerfile.dev -t mrl-verify:dev .` | exit 0, 3.49 GB |

## #82 compose stack (`deploy/docker-compose.yml`, project `mrl-stack`, lean image, `--no-build`)

| Item | Result |
| --- | --- |
| 1 launch | `app` healthy, `/health` 200; `otel-collector`, `phoenix`, `prometheus`, `grafana` running; `vllm-targets` exits 0 |
| 3 reproduce | default `mock` provider with no `MOCK_BASE_URL`: app log `ValueError: provider 'mock' needs MOCK_BASE_URL` on the first document |
| 3 fix | new opt-in `--profile mock` (runs `deploy/mock_openai.py`, healthy) + `MOCK_BASE_URL=http://mock:8000/v1` |
| 2 smoke | `scripts/smoke.sh` -> `SMOKE OK` (provider `mock`; document archived, Phoenix 200, Grafana health ok) |
| 4 passthrough | nothing optional set: none of `MAILROOM_ANCHOR*`, `MAILROOM_JEV_*`, `MAILROOM_TRACE_KEEP`, `MOCK_BASE_URL`, `JEV_API_KEY`, `TYPESAFE_API_KEY` in `app`; with `MOCK_BASE_URL` and `MAILROOM_ANCHOR=file` set both appear |
| 5 collector | kept `user: "0:0"` + read-only socket (decision: no default change for existing deployments; non-root recipe stays documented); `:8889/metrics` had 105 `container_*` series |
| 6 split-watcher | `MAILROOM_EMBED_WATCHER=0` in `app`; watcher `Healthcheck.Test=["NONE"]`; a file with unique content dropped into the watcher reached `archived` with no mention in the app log; `smoke.sh` -> `SMOKE OK` in this mode |
| 6 local-llm, gpu | `config -q` only (see top) |
| 7 dev compose | `scripts/dev.sh up` (project `mrl-dev`): app, phoenix, prometheus, grafana all 200 in `status`; `MAILROOM_ALLOW_UNAUTHENTICATED_BIND=1`; `scripts/dev.sh smoke` -> `DEV SMOKE OK`; `reset` leaves no volumes |
| 8 volumes | `down` keeps `mrl-stack_mailroom_data`; after `up` documents 1 -> 1; `mailroom audit verify` -> `chain: ok (5 entries ...)`, exit 0; `down -v` leaves 0 volumes |
| 9 exposure | published: `127.0.0.1:3000, 4317, 4318, 6006, 8000, 8889, 9090`; nothing on `0.0.0.0` |

## #83 sandbox container (`deploy/docker-compose.sandbox.yml`, project `mrl-sandbox`, `APP_UID=501`)

| Step | Result |
| --- | --- |
| 1-2 | compose `up -d --build`: 106 s, image 2.85 GB, `healthy` |
| 3 | `/health`, `/api/sandbox/v1/status`, `/api/sandbox/v1/messages` 200 JSON; `/ui` 200 HTML; `/ui/route.js` 200 JavaScript |
| 4 | `content-security-policy: default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'`; no CORS header; 0 inline `style=` and 0 literal `http(s)://` in UI assets |
| 5 | `scripts/tui_inbox_check.mjs` (system Chrome via `playwright-core`, host API from `scripts/tui_dev.sh` with `MAILROOM_SANDBOX_URL` = the container): first run 21/22, the failure a console 404 for `/favicon.ico`; fixed (inline icon) and after a rebuild `all checks passed` (deep link, Ingress tab, mailbox dock open with the correspondent filter, new row within 4 s without a reload, no token in any URL, no page errors) |
| 6 | `CapDrop=[ALL]`, `no-new-privileges:true`, `User=app` (uid 501), tmpfs `/tmp`, port `127.0.0.1:8100`; `/state` writable, `/usr/local` not; command carries `--egress closed` |
| 7 | default: starts unauthenticated on loopback; with `MAILROOM_API_TOKEN=x SANDBOX_ALLOW_UNAUTH=0`: `/status` 401 without, 200 with the bearer, `/health` 200; `SANDBOX_BIND=0.0.0.0 scripts/sandbox.sh up` (no `--expose`) exit 1 `non-loopback SANDBOX_BIND requires --expose and MAILROOM_API_TOKEN` |
| 8 | inject all -> 9 messages, 9 after `restart`; `POST /reset` 200; 0 `ledger_write_failed`, 0 `Traceback` in the logs |
| 9 | v0.5.0 asset sha256 `7a32e86e...0d91d135` (matches `sandbox/content.lock`); `content pull --from-bundle` ok; container with `SANDBOX_CONTENT=/content` lists 88 scenarios |
| 10 | `down -v`; image and build cache removed |

## #84 automation (`scripts/docker_smoke.sh`)

| Run | Result |
| --- | --- |
| green | `app image ok`, `sandbox image ok`, `compose ok`, `DOCKER SMOKE OK`, exit 0; no `mrl-smoke*` image, container or volume left |
| `COPY schemas` removed (`DOCKER_SMOKE_APP_DOCKERFILE` = a copy without the line) | `failed to solve: process "/bin/sh -c uv sync --frozen --no-dev --no-editable ${UV_EXTRAS}" ... exit code: 1`; `DOCKER SMOKE FAIL: app image build`, exit 1 |
| `DOCKER_HOST=unix:///nonexistent.sock` | `docker_smoke: Docker daemon unreachable`, exit 2; `pytest -m docker` -> 1 skipped |
| `shellcheck scripts/docker_smoke.sh` | clean |
| `actionlint .github/workflows/docker-smoke.yml` | clean; `actions/checkout` v4.2.2 = `11bd7190...`, `actions/upload-artifact` v4.4.3 = `b4b15b8c...` (checked with `gh api .../commits/<tag>`) |

The first green attempt failed only in the script's own compose check (it expected an unset passthrough key to be
absent; `docker compose config` renders it as `null`, which compose does not forward, as item 4 shows); the check was
corrected and the full run above is after that fix.
