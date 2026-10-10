# Docker Deployment Guide — mailroom-reloaded

This guide covers deployment of mailroom-reloaded using Docker Compose, including development, production, and sandbox modes.

---

## Quick Start (Development)

### Prerequisites
- Docker & Docker Compose (v2.20+)
- Python 3.11 (for local `uv` commands, optional for container-only setups)
- Git

### 1-Minute Dev Setup

```bash
cd mailroom-reloaded
cp .env.example .env

# Edit .env for your provider (default: mock)
# For real provider: set DEFAULT_PROVIDER (openrouter/vllm/llamafile) and credentials

# Start the full dev stack
docker compose -f deploy/docker-compose.dev.yml up -d --build

# Verify health
docker compose -f deploy/docker-compose.dev.yml ps
curl http://127.0.0.1:8000/health

# View logs
docker compose -f deploy/docker-compose.dev.yml logs -f app
```

**Services up:**
- **App (API + hot reload):** http://127.0.0.1:8000 → `/ui` for the runs dashboard
- **Watcher:** processes inbox files continuously
- **Phoenix (traces):** http://127.0.0.1:6006
- **Prometheus:** http://127.0.0.1:9090
- **Grafana:** http://127.0.0.1:3000 (admin/admin)

### Adding CPU-only Local LLM (llamafile)

```bash
# Start with llamafile profile + specify provider
docker compose -f deploy/docker-compose.dev.yml --profile local-llm up -d --build
export DEFAULT_PROVIDER=llamafile
curl -X POST http://127.0.0.1:8000/v1/documents -F "file=@test.txt"
```

---

## Production Deployment

### Setup

```bash
# 1. Prepare .env with real credentials
cp .env.example .env
# Edit: set MAILROOM_API_TOKEN, GRAFANA_ADMIN_PASSWORD, provider credentials

# 2. Build the production image (includes ModernBERT by default)
docker build -f deploy/Dockerfile -t mailroom-reloaded:latest .

# 3. Test locally
MAILROOM_API_TOKEN=test123 docker compose -f deploy/docker-compose.yml up -d --build

# 4. Smoke test
curl -H "Authorization: Bearer test123" http://127.0.0.1:8000/health
```

### Topology

**Default (no profile):** API + Watcher + Observability
```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

**With GPU vLLM:**
```bash
export VLLM_MODEL=Qwen/Qwen3-8B-AWQ
docker compose -f deploy/docker-compose.yml --profile gpu up -d --build
```

**Separate Watcher (scale horizontally):**
```bash
export MAILROOM_EMBED_WATCHER=0  # disable embedded watcher on app
docker compose -f deploy/docker-compose.yml --profile split-watcher up -d --build
# Now scale the watcher container:
docker compose -f deploy/docker-compose.yml --profile split-watcher up -d --scale watcher=3
```

### Environment Variables (Production)

**Required:**
- `MAILROOM_API_TOKEN` — Bearer token for `/v1` routes (min 16 chars recommended)
- `GRAFANA_ADMIN_PASSWORD` — Grafana admin password
- `DEFAULT_PROVIDER` — llm provider: mock, openrouter, vllm, llamafile

**Common:**
- `OPENROUTER_API_KEY` — OpenRouter credentials (if provider=openrouter)
- `HF_TOKEN` — HuggingFace token (for private models in vLLM)
- `VLLM_MODEL` — Model name for vLLM (default: Qwen/Qwen3-8B-AWQ)
- `MAILROOM_TRACE_KEEP` — Span retention policy: pinned, all, recent:N (default: pinned)

**Advanced:**
- `MAILROOM_ANCHOR` — External ledger anchor: none, export, postgres, supabase
- `MAILROOM_JEV_PROVIDER` — Jev decision model: off, openrouter, typesafe, local
- `MAILROOM_TRACE_MASK` — Otel sampling mask (0-1 float; 0 = no spans, 1 = all)

### Data Persistence

Production uses named volumes:
- `mailroom_data:/data` — documents, manifests, audit log, models
- `hf_cache:/hf_cache` — HuggingFace model cache
- `grafana_data:/var/lib/grafana` — Grafana dashboards and settings
- `prometheus_data:/prometheus` — Prometheus timeseries

**Backup:**
```bash
# Export mailroom_data volume to a tarball
docker run --rm -v mailroom_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/mailroom-data.tar.gz -C /data .
```

**Restore:**
```bash
# Import from tarball
docker run --rm -v mailroom_data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/mailroom-data.tar.gz -C /data
```

### Horizontal Scaling

**Watchers:** Run multiple watcher instances on the same `mailroom_data` volume:
```bash
docker compose -f deploy/docker-compose.yml --profile split-watcher up -d --scale watcher=5
```
(The watcher uses file-based locking to ensure each document is claimed by exactly one worker.)

**API:** Run multiple app instances behind a load balancer (all share `mailroom_data`):
```bash
# Remove embedded watcher, scale app
docker compose -f deploy/docker-compose.yml down
export MAILROOM_EMBED_WATCHER=0
docker compose -f deploy/docker-compose.yml --profile split-watcher up -d --scale app=3 --scale watcher=2
```

### Health Checks

All containers define `healthcheck` intervals. Monitor with:
```bash
docker compose -f deploy/docker-compose.yml ps
# HEALTHY = all ready; UNHEALTHY = investigate logs
docker compose -f deploy/docker-compose.yml logs app
```

---

## Sandbox (Offline Ingress Simulation)

The sandbox server runs the mailroom pipeline end-to-end with offline mocks, no observability, and no real LLM calls. Ideal for demos and integration testing.

### Start Sandbox

```bash
# Build and start
docker compose -f deploy/docker-compose.sandbox.yml up -d --build

# UI
open http://127.0.0.1:8100/ui

# Upload a document
curl -X POST http://127.0.0.1:8100/v1/documents \
  -F "file=@docs/sample_letter.txt"
```

### Sandbox Modes

| Mode | SANDBOX_EGRESS | Use Case |
|------|---|---|
| `closed` (default) | ✗ No outbound mail | Local testing, CI/CD |
| `capture` | ✓ Virtual outbox at `/state/outbox` | Inspect generated mail |

### Custom Content Bundle

```bash
# Pull content from remote
uv run mailroom sandbox content pull --revision main --out ./my-content

# Serve custom content
export SANDBOX_CONTENT_DIR=$(pwd)/my-content
docker compose -f deploy/docker-compose.sandbox.yml up -d --build
```

---

## Dockerfile Architecture

### Production (`deploy/Dockerfile`)
**Multi-stage, optimized for size and startup:**
- **builder stage:** uv + dependencies (cached, ~500MB intermediate)
- **model stage:** ModernBERT bundle from HuggingFace (optional, ~2GB)
- **runtime stage:** Slim Python + venv + models (~1.2GB final, or ~200MB without BERT)

**Layer cache strategy:**
1. Copy `pyproject.toml` + `uv.lock` → install deps (invalidates on dependency change)
2. Copy `src/` + `schemas/` → sync project (fast rebuild for code changes)

**Build arguments:**
```bash
# Lean build (no BERT, for OpenRouter/vLLM):
docker build --build-arg ML_BUILD_NONE=1 --build-arg UV_EXTRAS= -f deploy/Dockerfile .

# Standard build (with BERT):
docker build -f deploy/Dockerfile .
```

### Development (`deploy/Dockerfile.dev`)
**Single stage, built for hot reload:**
- Installs `dev` extra (pytest, ruff, respx)
- Source bind-mounted at `/app/src`
- `uvicorn --reload` watches and reloads on file change
- Supports in-container test runs: `docker compose exec app pytest`

### Sandbox (`deploy/Dockerfile.sandbox`)
**Minimal, self-contained, offline:**
- Only `sandbox` extra
- Source COPIED in (no bind mount)
- ~500MB image
- Runs end-to-end pipeline with mocks

---

## Troubleshooting

### App won't start: "Refusing to bind to 0.0.0.0 without MAILROOM_API_TOKEN"

**Cause:** Binding off-loopback without auth (security policy).

**Fix:** Set `MAILROOM_API_TOKEN` in `.env` or pass `--env-file`:
```bash
echo "MAILROOM_API_TOKEN=my-secret-token-here" > .env
docker compose -f deploy/docker-compose.yml up -d
```

### "database is locked" errors in logs

**Cause:** Multiple workers (app + watcher) writing to SQLite concurrently.

**Fix:** This is normal and handled gracefully (the bug fixes include retry logic). If frequent, consider splitting to separate containers:
```bash
export MAILROOM_EMBED_WATCHER=0
docker compose -f deploy/docker-compose.yml --profile split-watcher up -d
```

### OOM (Out of Memory) on vLLM container

**Cause:** Model too large for available VRAM.

**Fix:** Reduce tensor parallelism or use a smaller model:
```bash
export VLLM_MODEL=Qwen/Qwen3-2B
export VLLM_TP=1
docker compose -f deploy/docker-compose.yml --profile gpu up -d
```

### Grafana dashboards not showing data

**Cause:** Prometheus needs time to scrape metrics.

**Fix:** Wait ~30s and refresh. Check Prometheus health:
```bash
curl http://127.0.0.1:9090/-/healthy
docker compose -f deploy/docker-compose.yml logs prometheus | tail -20
```

### Development: "Address already in use" on port 8000

**Cause:** Another container or process using the port.

**Fix:**
```bash
# Find and stop conflicting container
docker ps | grep 8000
docker stop <container-id>

# Or use a different port
docker compose -f deploy/docker-compose.dev.yml down
export APP_PORT=8001
docker run -p 127.0.0.1:${APP_PORT}:8000 mailroom-reloaded-dev:latest
```

---

## Performance Tuning

### vLLM on GPU

```bash
# Multi-GPU tensor parallelism
export VLLM_TP=2 VLLM_DP=2
export VLLM_MODEL=Qwen/Qwen3-32B-AWQ
export VLLM_MAX_LEN=65536
docker compose -f deploy/docker-compose.yml --profile gpu up -d
```

### Span Retention (reduce storage footprint)

```bash
# Keep only 30 days of spans (from today backwards)
export MAILROOM_TRACE_KEEP=recent:30
docker compose -f deploy/docker-compose.yml up -d
```

### CPU-only Production (modal.com vLLM)

For serverless deployment without GPUs, use Modal or similar:
```bash
export VLLM_BASE_URL=https://modal-vllm-endpoint.com/v1
export OPENROUTER_API_KEY=...
docker compose -f deploy/docker-compose.yml up -d
```

---

## Cleanup & Maintenance

### Stop and Remove All

```bash
docker compose -f deploy/docker-compose.yml down -v  # -v deletes volumes
docker system prune --all  # removes dangling images
```

### View Full Logs (including startup)

```bash
docker compose -f deploy/docker-compose.yml logs --tail=100 app
docker compose -f deploy/docker-compose.dev.yml logs --tail=100 --follow
```

### Rebuild Single Container

```bash
docker compose -f deploy/docker-compose.yml build --no-cache app
docker compose -f deploy/docker-compose.yml up -d app
```

---

## CI/CD Integration

### Build & Push to Registry

```bash
# Build production image
docker build -f deploy/Dockerfile -t myregistry.azurecr.io/mailroom:latest .

# Push
docker push myregistry.azurecr.io/mailroom:latest

# Deploy
docker compose -f deploy/docker-compose.yml set image app=myregistry.azurecr.io/mailroom:latest
docker compose -f deploy/docker-compose.yml up -d
```

### GitHub Actions Example

```yaml
name: Deploy Mailroom

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v2
      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: .
          file: ./deploy/Dockerfile
          push: true
          tags: myregistry.azurecr.io/mailroom:${{ github.sha }}
          cache-from: type=registry
          cache-to: type=registry,mode=max
```

---

## References

- **Main topology:** `deploy/docker-compose.yml`
- **Dev topology:** `deploy/docker-compose.dev.yml`
- **Sandbox topology:** `deploy/docker-compose.sandbox.yml`
- **Production guide:** `deploy/README.md`
- **Configuration reference:** `docs/CONFIGURATION.md`
- **Architecture:** `docs/ARCHITECTURE.md`

---

**Last Updated:** 2025-01-09  
**Version:** mailroom-reloaded 0.2.0
