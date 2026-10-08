# mailroom-reloaded

The compressed and deployment-ready package of the Digital Mailroom, built on CrewAI Flows.

## Setup

```bash
uv sync --extra dev
uv run pytest
```

Providers: `llamafile`, `openrouter`, `vllm`, `mock` (default). Copy `.env.example` to `.env` to configure.
The taxonomy (classes, confidence thresholds, per-class run conditions) lives in
`src/mailroom_reloaded/config/taxonomy.yaml`.

Extras: `bert`, `eval`, `embeddings`, `deploy`, `dev`, `parity`.
