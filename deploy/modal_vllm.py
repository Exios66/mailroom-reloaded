"""Modal-deployed vLLM for mailroom-reloaded (OpenAI-compatible, with /metrics).

One Modal app, ``mailroom-vllm``, serving one model through ``@modal.web_server``.
vLLM exposes ``/v1/*`` and ``/metrics`` on the same port, so the app's
``VLLM_BASE_URL`` and the Collector's ``VLLM_METRICS_URLS`` share one host.

Deploy-time env (read when this file is imported by ``modal deploy``):

    MODAL_GPU               GPU type (default L4)
    MODAL_GPU_COUNT         GPUs per replica (default 1); also the tensor-parallel size
    MODAL_MAX_CONTAINERS    replica cap (default 1)
    MODAL_SCALEDOWN_WINDOW  idle seconds before scale-to-zero (default 300)
    VLLM_MODEL              Hub model id (default Qwen/Qwen3-8B-AWQ)
    VLLM_MAX_LEN            --max-model-len (default 32768)
    VLLM_API_KEY            bearer token, read from the Modal secret ``mailroom-vllm-api-key``

Engine flags follow the SAND-37 conditions: prefix caching, fp8 KV cache.

    modal deploy deploy/modal_vllm.py
    modal app stop mailroom-vllm        # teardown

``deploy_config()`` is pure and the Modal objects are only built when the
``modal`` package (the ``deploy`` extra) is importable, so tests need neither.
"""

import json
import os
import subprocess
import threading
import time
import urllib.request

try:
    import modal
except ImportError:  # the deploy extra is optional
    modal = None

APP_NAME = "mailroom-vllm"
SERVER_PORT = 8000
VLLM_VERSION = "0.29.0"  # matches vllm/vllm-openai:v0.29.0 in docker-compose
SECRET_NAME = "mailroom-vllm-api-key"
HF_CACHE_MOUNT = "/root/.cache/huggingface"
STARTUP_TIMEOUT_SECONDS = 20 * 60


def deploy_config() -> dict:
    """Return the effective deploy settings from the environment (pure)."""
    gpu = os.environ.get("MODAL_GPU", "").strip() or "L4"
    count = int(os.environ.get("MODAL_GPU_COUNT", "").strip() or 1)
    max_containers = int(os.environ.get("MODAL_MAX_CONTAINERS", "").strip() or 1)
    scaledown = int(os.environ.get("MODAL_SCALEDOWN_WINDOW", "").strip() or 300)
    model = os.environ.get("VLLM_MODEL", "").strip() or "Qwen/Qwen3-8B-AWQ"
    max_len = int(os.environ.get("VLLM_MAX_LEN", "").strip() or 32768)
    flags = [
        "--enable-prefix-caching",
        "--kv-cache-dtype",
        "fp8",
        "--tensor-parallel-size",
        str(count),
        "--max-model-len",
        str(max_len),
    ]
    return {
        "app_name": APP_NAME,
        "gpu": gpu,
        "gpu_count": count,
        "gpu_spec": gpu if count == 1 else f"{gpu}:{count}",
        "model": model,
        "max_len": max_len,
        "max_containers": max_containers,
        "scaledown_window": scaledown,
        "port": SERVER_PORT,
        "vllm_version": VLLM_VERSION,
        "flags": flags,
    }


CFG_ENV = "MAILROOM_VLLM_CFG"


def baked_env(cfg: dict) -> dict[str, str]:
    """Env baked into the image so the container sees the deploy-time config."""
    return {CFG_ENV: json.dumps(cfg)}


def resolved_config() -> dict:
    """Container side: prefer the baked config, else read the environment."""
    baked = os.environ.get(CFG_ENV, "").strip()
    return json.loads(baked) if baked else deploy_config()


def build_command(cfg: dict) -> list[str]:
    """Assemble the ``vllm serve`` argv for a config."""
    return [
        "vllm",
        "serve",
        cfg["model"],
        "--host",
        "0.0.0.0",
        "--port",
        str(cfg["port"]),
        *cfg["flags"],
    ]


app = None

if modal is not None:
    _cfg = deploy_config()
    app = modal.App(APP_NAME)
    _hf_cache = modal.Volume.from_name("mailroom-hf-cache", create_if_missing=True)
    _image = modal.Image.debian_slim(python_version="3.12").uv_pip_install(
        f"vllm=={VLLM_VERSION}", "huggingface_hub"
    ).env(baked_env(_cfg))

    def _commit_when_healthy(port: int) -> None:
        """Persist downloaded weights to the cache Volume once /health answers."""
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                if urllib.request.urlopen(url, timeout=5).status == 200:
                    _hf_cache.commit()
                    return
            except OSError:
                pass
            time.sleep(5)

    @app.function(
        image=_image,
        gpu=_cfg["gpu_spec"],
        volumes={HF_CACHE_MOUNT: _hf_cache},
        secrets=[modal.Secret.from_name(SECRET_NAME)],
        scaledown_window=_cfg["scaledown_window"],
        max_containers=_cfg["max_containers"],
        timeout=60 * 30,
    )
    @modal.concurrent(max_inputs=32)
    @modal.web_server(port=SERVER_PORT, startup_timeout=STARTUP_TIMEOUT_SECONDS)
    def serve() -> None:
        """Start vLLM; /v1/* and /metrics are both served on SERVER_PORT."""
        # VLLM_API_KEY from the secret is read natively by vLLM (401 without bearer).
        cfg = resolved_config()
        subprocess.Popen(build_command(cfg))
        threading.Thread(target=_commit_when_healthy, args=(cfg["port"],), daemon=True).start()
