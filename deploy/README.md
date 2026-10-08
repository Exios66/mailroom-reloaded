# Modal vLLM deploy

`deploy/modal_vllm.py` serves an OpenAI-compatible vLLM (`/v1/*`) and its Prometheus `/metrics` from one Modal web server, app name `mailroom-vllm`. Engine flags follow the SAND-37 conditions: `--enable-prefix-caching`, `--kv-cache-dtype fp8`, `--tensor-parallel-size <MODAL_GPU_COUNT>`, `--max-model-len ${VLLM_MAX_LEN}`. vLLM is pinned to 0.29.0, the same as the compose image.

## Settings

| Env | Default | Meaning |
| --- | --- | --- |
| `MODAL_GPU` | `L4` | GPU type |
| `MODAL_GPU_COUNT` | `1` | GPUs per replica; sets tensor parallelism |
| `MODAL_MAX_CONTAINERS` | `1` | Replica cap |
| `MODAL_SCALEDOWN_WINDOW` | `300` | Idle seconds before scale to zero |
| `VLLM_MODEL` | `Qwen/Qwen3-8B-AWQ` | Hub model id |
| `VLLM_MAX_LEN` | `32768` | Context window |

The bearer token comes from the Modal secret `mailroom-vllm-api-key` (key `VLLM_API_KEY`).

## Deploy

```bash
uv sync --extra deploy && modal token new      # once
modal secret create mailroom-vllm-api-key VLLM_API_KEY="$(openssl rand -hex 24)"
MODAL_GPU=L4 MODAL_GPU_COUNT=1 modal deploy deploy/modal_vllm.py
```

The URL is printed by `modal deploy`. Cold start takes a few minutes the first time (weights download); the app's retry backoff allows 90 s per attempt and transient errors do not consume the confidence retry budget.

## Point the app at it

```bash
DEFAULT_PROVIDER=vllm
VLLM_BASE_URL=https://<workspace>--mailroom-vllm-serve.modal.run/v1
VLLM_API_KEY=<the secret value>
VLLM_METRICS_URLS=https://<workspace>--mailroom-vllm-serve.modal.run/metrics
```

The Collector scrapes `/metrics` through `VLLM_METRICS_URLS`.

## SAND-37-style posture run

Two GPUs, tensor-parallel 2, high concurrency:

```bash
MODAL_GPU_COUNT=2 modal deploy deploy/modal_vllm.py
mailroom eval --mode specialist_cell --per-class 50 --gpus 2 --concurrency 32 --revision ed7576b6 --prompt-set sand37
```

`--gpus 2` makes the cost card use 2 x the GPU-hour price (L4 default $0.80 per GPU-hour).

## Teardown and spend check

```bash
modal app stop mailroom-vllm
modal app list            # confirm mailroom-vllm is stopped, no running containers
modal billing report --for today   # or check the Usage page in the Modal dashboard
```

Containers also scale to zero after `MODAL_SCALEDOWN_WINDOW` idle seconds, but stop the app when the run is finished.
