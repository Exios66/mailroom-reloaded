"""In-process offline OpenAI-compatible endpoint for the ``mock`` provider.

Mirrors ``deploy/mock_openai.py`` (fixed synthetic correspondence answers, no
inference) and adds request counters so the sandbox can assert the "fast path is
two structured LLM calls" invariant per document. It listens on an ephemeral
loopback port only.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException

__all__ = ["MockLLM", "build_mock_app"]

SORT = {
    "doc_type": "correspondence",
    "doc_subclass": "email",
    "confidence": 0.99,
    "doc_type_disagree": False,
    "doc_type_disagree_reason": None,
}
EXTRACT = {
    "sender": "alice@example.com",
    "recipient": "bob@example.com",
    "additional_recipients": ["carol@example.com"],
    "communication_type": "email",
    "communication_date": "2026-01-02",
    "demand_amount": 1000.0,
    "action_items": ["reply by Friday"],
    "urgency": "normal",
    "intent": "request",
    "subject_matter": "synthetic dev fixture",
    "keywords": ["fixture"],
    "confidence": 0.99,
}
_MARKER = re.compile(r"\[confidence:(\d?\.\d+)\]")


def _marker_confidence(body: dict) -> float | None:
    """Read the first confidence marker from chat messages and clamp it to [0, 1]."""
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        match = _MARKER.search(content) if isinstance(content, str) else None
        if match:
            return min(max(float(match.group(1)), 0.0), 1.0)
    return None


def build_mock_app(stats: dict[str, int] | None = None) -> FastAPI:
    """Build an offline completion API that records request counts in the given mapping."""
    stats = stats if stats is not None else {}
    app = FastAPI(title="sandbox-mock-llm")

    @app.get("/health")
    def health() -> dict:
        """Return the mock endpoint liveness status."""
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    def complete(body: dict) -> dict:
        """Return synthetic non-streaming completions for supported request schemas."""
        if body.get("stream"):
            raise HTTPException(400, "The mock supports non-streaming requests only")
        schema = (
            body.get("response_format", {}).get("json_schema", {}).get("schema", {})
        )
        properties = schema.get("properties", {})
        marker = _marker_confidence(body)
        if "doc_subclass" in properties:
            stats["structured"] = stats.get("structured", 0) + 1
            sort = dict(SORT, **({"confidence": marker} if marker is not None else {}))
            content = json.dumps({k: v for k, v in sort.items() if k in properties})
        elif "sender" in properties:
            stats["structured"] = stats.get("structured", 0) + 1
            extract = dict(
                EXTRACT, **({"confidence": marker} if marker is not None else {})
            )
            content = json.dumps(extract)
        elif schema:
            raise HTTPException(400, "The mock supports correspondence schemas only")
        else:
            stats["tool_discovery"] = stats.get("tool_discovery", 0) + 1
            content = "Ready for the structured response."
        return {
            "id": "chatcmpl-sandbox-mock",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "sandbox-mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return app


class MockLLM:
    """Run the mock on an ephemeral 127.0.0.1 port in a daemon thread."""

    def __init__(self) -> None:
        """Initialize counters and inactive loopback server resources."""
        self.stats: dict[str, int] = {}
        self.base_url = ""
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> MockLLM:
        """Start the mock on an ephemeral loopback port or raise if startup times out."""
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        self._sock = sock
        port = sock.getsockname()[1]
        config = uvicorn.Config(build_mock_app(self.stats), log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=lambda: self._server.run(sockets=[sock]),
            name="sandbox-mock-llm",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("mock LLM failed to start")
        self.base_url = f"http://127.0.0.1:{port}/v1"
        return self

    def stop(self) -> None:
        """Request server shutdown, join its thread, and close the listening socket."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()
        self._server = self._thread = self._sock = None

    def structured_calls(self) -> int:
        """Return the number of structured classification and extraction requests."""
        return self.stats.get("structured", 0)
