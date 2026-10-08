"""FakeOpenAI: a scripted OpenAI-compatible chat-completions server for tests.

Runs a FastAPI app under uvicorn in a background thread on a free port.
Responses are consumed from a FIFO queue; every request body is recorded in
``.requests``. Import the ``fake_openai`` fixture into a conftest.py:
``from fakes.openai_server import fake_openai  # noqa: F401``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class FakeOpenAI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._queue: deque[dict[str, Any]] = deque()
        self._reject_tools = False
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.app = self._build_app()

    # ---- scripting helpers -------------------------------------------------
    def reply(self, content: str, finish_reason: str = "stop") -> FakeOpenAI:
        self._queue.append({"kind": "reply", "content": content, "finish_reason": finish_reason})
        return self

    def tool_call(self, name: str, args: dict[str, Any]) -> FakeOpenAI:
        self._queue.append({"kind": "tool_call", "name": name, "args": args})
        return self

    def length_capped(self, content: str = '{"partial": ') -> FakeOpenAI:
        return self.reply(content, finish_reason="length")

    def fail(self, status: int, times: int = 1, message: str = "scripted failure") -> FakeOpenAI:
        for _ in range(times):
            self._queue.append({"kind": "fail", "status": status, "message": message})
        return self

    def reject_tools(self) -> FakeOpenAI:
        """Answer HTTP 400 to any request that carries ``tools``."""
        self._reject_tools = True
        return self

    def with_logprobs(self, tokens: list[tuple[str, float]]) -> FakeOpenAI:
        """Attach per-token logprobs to the most recently queued reply."""
        self._queue[-1]["logprobs"] = tokens
        return self

    # ---- server ------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            with self._lock:
                self.requests.append(body)
                if self._reject_tools and body.get("tools"):
                    return JSONResponse(
                        {"error": {"message": "tools are not supported", "type": "invalid_request_error"}},
                        status_code=400,
                    )
                if not self._queue:
                    return JSONResponse({"error": {"message": "script exhausted"}}, status_code=418)
                item = self._queue.popleft()
            if item["kind"] == "fail":
                return JSONResponse({"error": {"message": item["message"]}}, status_code=item["status"])
            return JSONResponse(self._completion(item, body))

        return app

    @staticmethod
    def _completion(item: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": None}
        finish = "stop"
        logprobs = None
        if item["kind"] == "tool_call":
            message["tool_calls"] = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": item["name"], "arguments": json.dumps(item["args"])},
                }
            ]
            finish = item.get("finish_reason", "tool_calls")
        else:
            message["content"] = item["content"]
            finish = item["finish_reason"]
            if item.get("logprobs") and body.get("logprobs"):
                logprobs = {
                    "content": [
                        {"token": t, "logprob": lp, "bytes": None, "top_logprobs": []}
                        for t, lp in item["logprobs"]
                    ]
                }
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": logprobs}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("FakeOpenAI failed to start")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def fake_openai():
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()
