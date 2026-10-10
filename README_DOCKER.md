# mailroom-reloaded: Deployment & Development

## Overview

mailroom-reloaded is a production-ready document processing pipeline with:
- **7 critical bug fixes** applied (SHA256 verification, race conditions, retry logic, etc.)
- **3 optimized Dockerfiles** for production, development, and sandbox environments
- **3 docker-compose topologies** for dev, production, and offline testing
- **Hot-reload development** with uvicorn and bind-mounted source
- **Full observability** (Phoenix, Prometheus, Grafana)
- **Horizontal scaling** (multi-worker watchers, load-balanced API)

---

## Quick Start (< 5 minutes)

### 1. Validate Setup
```bash
bash scripts/validate-docker.sh
```

### 2. Start Development Stack
```bash
bash scripts/quickstart.sh up
```

This will:
- Create `.env` with sensible defaults (mock provider, dev token)
- Build Docker images
- Start 8 services: app + watcher + mock + otel-collector + phoenix + prometheus + grafana
- Open endpoints at `http://127.0.0.1:8000`

### 3. Verify Everything Works
```bash
# Check status
bash scripts/quickstart.sh status

# Follow app logs
bash scripts/quickstart.sh logs

# Upload a test document
curl -X POST http://127.0.0.1:8000/v1/documents \
  -F "file=@docs/fixtures/letter.txt"
```

### 4. Access Services
- **API & UI:** http://127.0.0.1:8000
- **Phoenix traces:** http://127.0.0.1:6006
- **Prometheus:** http://127.0.0.1:9090
- **Grafana:** http://127.0.0.1:3000 (admin/admin)

---

## Bug Fixes Applied

All 7 critical bugs have been patched:

| Bug | Issue | Fix |
|-----|-------|-----|
| 1 | SHA256 upload mismatch | Post-enqueue verification |
| 2 | Race in `_locate_parked()` | File re-check after hash |
| 3 | Missing subclass validation | Added type/content checks |
| 4 | Unbounded retry loop | Exponential backoff |
| 5 | Unsafe attribute access | Safe `getattr()` with fallbacks |
| 6 | Timeless items collapse | Changed return type to `Optional` |
| 7 | Catalog retry logic | Transient error retry loop |

**Status:** All tested and verified. See `BUGFIXES_SUMMARY.md` for details.

---

## Docker Images

### Production (`deploy/Dockerfile`)
**Size:** ~1.2GB (or ~200MB without BERT)
- Multi-stage build: builder → model → runtime
- Non-root user (uid 10001)
- Optimized layer caching
- ModernBERT bundle included by default

```bash
# With BERT (default)
docker build -f deploy/Dockerfile -t mailroom:latest .

# Lean (no BERT, for OpenRouter/vLLM)
docker build --build-arg ML_BUILD_NONE=1 \
  --build-arg UV_EXTRAS= -f deploy/Dockerfile .
```

### Development (`deploy/Dockerfile.dev`)
**Size:** ~400MB
- Includes `dev` extra (pytest, ruff, respx)
- Hot-reload ready (uvicorn --reload)
- Source bind-mounted at `/app/src`

```bash
docker build -f deploy/Dockerfile.dev -t mailroom-dev:latest .
```

### Sandbox (`deploy/Dockerfile.sandbox`)
**Size:** ~500MB
- Minimal, offline, self-contained
- Only `sandbox` extra
- Perfect for demos and CI/CD

```bash
docker build -f deploy/Dockerfile.sandbox -t mailroom-sandbox:latest .
```

---

## Docker Compose Configurations

### Development (`deploy/docker-compose.dev.yml`)
**Best for:** Local development with hot reload

```bash
docker compose -f deploy/docker-compose.dev.yml up -d --build

# With llamafile (CPU LLM)
docker compose -f deploy/docker-compose.dev.yml --profile local-llm up -d --build
```

**Services:**
- `app` (uvicorn --reload, port 8000)
- `watcher` (document processor)
- `mock` (synthetic LLM)
- `otel-collector`, `phoenix`, `prometheus`, `grafana`
- `llamafile` (optional, CPU LLM)

**Data:** Bind-mounted from `./data` (inspect locally)

### Production (`deploy/docker-compose.yml`)
**Best for:** Deployed environments

```bash
# Set required env vars
export MAILROOM_API_TOKEN=secret-token-here
export GRAFANA_ADMIN_PASSWORD=secure-password
export DEFAULT_PROVIDER=openrouter  # or vllm, llamafile

docker compose -f deploy/docker-compose.yml up -d --build

# With GPU vLLM
docker compose -f deploy/docker-compose.yml --profile gpu up -d

# Horizontal scaling (3 app, 5 watchers)
docker compose -f deploy/docker-compose.yml --profile split-watcher \
  up -d --scale app=3 --scale watcher=5
```

**Data:** Named volumes (persistent, portable)

### Sandbox (`deploy/docker-compose.sandbox.yml`)
**Best for:** Offline demos, testing

```bash
docker compose -f deploy/docker-compose.sandbox.yml up -d --build

open http://127.0.0.1:8100/ui
```

**Services:**
- `sandbox` (offline pipeline, port 8100)
- No observability, no LLM

---

## Environment Configuration

### Setup `.env`
```bash
cp .env.example .env
# Edit with your settings
```

### Key Variables

**Authentication:**
- `MAILROOM_API_TOKEN` — Bearer token for `/v1` routes (required for production)

**LLM Providers:**
- `DEFAULT_PROVIDER` — mock (default), openrouter, vllm, llamafile
- `OPENROUTER_API_KEY` — OpenRouter credentials (if using OpenRouter)
- `VLLM_BASE_URL` — vLLM endpoint (if using vLLM)

**Observability:**
- `MAILROOM_TRACE_KEEP` — Span retention: pinned, all, recent:N (default: pinned)
- `MAILROOM_TRACE_MASK` — OTel sampling (0.0-1.0; 0 = none, 1 = all)

**Jev (Decision Model):**
- `MAILROOM_JEV_PROVIDER` — off, openrouter, typesafe, local
- `MAILROOM_JEV_MODEL` — Model name
- `TYPESAFE_API_KEY` — TypeSafe System One credentials

**Docker:**
- `GRAFANA_ADMIN_USER` — Grafana username (default: admin)
- `GRAFANA_ADMIN_PASSWORD` — Grafana password (required for production)
- `APP_UID` — Non-root user ID in containers (default: 1000)

See `.env.example` for all variables.

---

## Common Commands

### Development Workflow

```bash
# Start stack
bash scripts/quickstart.sh up

# Make code changes (auto-reloaded)
vim src/mailroom_reloaded/api/app.py

# View logs
bash scripts/quickstart.sh logs

# Run tests in container
docker compose -f deploy/docker-compose.dev.yml exec app pytest

# Check status
bash scripts/quickstart.sh status

# Stop stack
bash scripts/quickstart.sh down
```

### Production Deployment

```bash
# Build production image
docker build -f deploy/Dockerfile -t mailroom:v0.2.0 .

# Push to registry
docker tag mailroom:v0.2.0 myregistry.azurecr.io/mailroom:v0.2.0
docker push myregistry.azurecr.io/mailroom:v0.2.0

# Deploy the pushed image
cat > deploy/docker-compose.registry.yml <<'YAML'
services:
  app:
    image: myregistry.azurecr.io/mailroom:v0.2.0
YAML
MAILROOM_API_TOKEN=prod-secret GRAFANA_ADMIN_PASSWORD=prod-pass \
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.registry.yml \
  up -d --no-build --pull always
```

### Scaling

```bash
# Scale watchers (document processing)
docker compose -f deploy/docker-compose.yml --profile split-watcher \
  up -d --scale watcher=10

# Scale app (API servers behind load balancer)
docker compose -f deploy/docker-compose.yml \
  up -d --scale app=3
```

### Cleanup

```bash
# Stop stack
bash scripts/quickstart.sh down

# Full reset (deletes volumes and data)
bash scripts/quickstart.sh reset

# Remove all images
docker image rm mailroom-reloaded:latest mailroom-reloaded-dev:latest mailroom-sandbox:latest
```

---

## Troubleshooting

### "Address already in use" on port 8000
```bash
# Kill the process
lsof -ti:8000 | xargs kill -9
# Or use different port
docker run -p 127.0.0.1:8001:8000 mailroom-reloaded-dev:latest
```

### Docker daemon not responding
```bash
# Restart Docker
# macOS:
open -a Docker

# Linux:
sudo systemctl restart docker
```

### "database is locked" in app logs
This is normal and handled gracefully (retries with backoff). If frequent, split app and watcher:
```bash
export MAILROOM_EMBED_WATCHER=0
docker compose -f deploy/docker-compose.dev.yml up -d
```

### vLLM OOM on GPU
Reduce tensor parallelism:
```bash
export VLLM_TP=1 VLLM_MODEL=Qwen/Qwen3-2B
docker compose -f deploy/docker-compose.yml --profile gpu up -d
```

### Grafana showing no data
Wait 30s for Prometheus to scrape, then:
```bash
curl http://127.0.0.1:9090/-/healthy
docker compose -f deploy/docker-compose.dev.yml logs prometheus | tail -20
```

---

## Documentation

- **Deployment details:** `DOCKER_DEPLOYMENT.md`
- **Bug fixes:** `BUGFIXES_SUMMARY.md`, `PATCH_REFERENCE.md`
- **Architecture:** `docs/ARCHITECTURE.md`
- **Configuration reference:** `docs/CONFIGURATION.md`
- **Project README:** `README.md`

---

## Next Steps

1. ✅ **Run validation:** `bash scripts/validate-docker.sh`
2. ✅ **Start dev:** `bash scripts/quickstart.sh up`
3. ✅ **Verify health:** `bash scripts/quickstart.sh status`
4. ✅ **Test upload:** `curl -X POST http://127.0.0.1:8000/v1/documents -F "file=@test.txt"`
5. 📚 **Read the guides:** `DOCKER_DEPLOYMENT.md`

---

**Version:** mailroom-reloaded v0.2.0 + 7 bug fixes  
**Last Updated:** 2025-01-09  
**Status:** ✅ Ready for development and production deployment
