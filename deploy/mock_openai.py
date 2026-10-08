"""Dev-only OpenAI endpoint: fixed synthetic correspondence, no inference."""

import json
import re

from fastapi import FastAPI, HTTPException

app = FastAPI()

SORT = {
    "doc_type": "correspondence", "doc_subclass": "email", "confidence": 0.99,
    "doc_type_disagree": False, "doc_type_disagree_reason": None,
}
EXTRACT = {
    "sender": "alice@example.com", "recipient": "bob@example.com",
    "additional_recipients": ["carol@example.com"], "communication_type": "email",
    "communication_date": "2026-01-02", "demand_amount": 1000.0,
    "action_items": ["reply by Friday"], "urgency": "normal", "intent": "request",
    "subject_matter": "synthetic dev fixture", "keywords": ["fixture"], "confidence": 0.99,
}


_MARKER = re.compile(r"\[confidence:(\d?\.\d+)\]")


def _marker_confidence(body: dict) -> float | None:
    """A ``[confidence:0.NN]`` marker in any message overrides the confidence."""
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        match = _MARKER.search(content) if isinstance(content, str) else None
        if match:
            return min(max(float(match.group(1)), 0.0), 1.0)
    return None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
def complete(body: dict):
    if body.get("stream"):
        raise HTTPException(400, "The dev mock supports non-streaming requests only")
    schema = body.get("response_format", {}).get("json_schema", {}).get("schema", {})
    properties = schema.get("properties", {})
    marker = _marker_confidence(body)
    if "doc_subclass" in properties:
        sort = dict(SORT, **({"confidence": marker} if marker is not None else {}))
        content = json.dumps({key: value for key, value in sort.items() if key in properties})
    elif "sender" in properties:
        extract = dict(EXTRACT, **({"confidence": marker} if marker is not None else {}))
        content = json.dumps(extract)
    elif schema:
        raise HTTPException(400, "The dev mock supports correspondence schemas only")
    else:
        # Tool-discovery phase: proceed directly to the structured final turn.
        content = "Ready for the structured response."
    return {
        "id": "chatcmpl-dev-mock", "object": "chat.completion", "created": 0,
        "model": body.get("model", "dev-mock"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
