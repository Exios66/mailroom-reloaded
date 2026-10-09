"""Transient-failure retry for LLM calls (ported from llm-mailroom, minus gateway and free swarm).

Retried: connection errors, timeouts, 429, and 5xx. A 503 is treated as an
endpoint cold start (Modal scale-to-zero) and backs off on the long
``cold_start_s`` ladder. Other 4xx are never retried, except Alibaba/Qwen's
intermittent ``json_object`` 400, which succeeds on a plain retry.

Retries are transport-level: they never consume the confidence retry budget.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import structlog
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)
from opentelemetry import trace

from mailroom_reloaded.settings import load_taxonomy

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_JSON_MODE_400_MARKERS = ("must contain the word 'json'",)

# Patched in tests so backoff does not really sleep.
_sleep: Callable[[float], None] = time.sleep


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        return max(0.0, float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_json_mode_400(exc: Exception) -> bool:
    return isinstance(exc, BadRequestError) and any(m in str(exc) for m in _JSON_MODE_400_MARKERS)


def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS or _is_json_mode_400(exc)
    return False


def retry_sleep_seconds(exc: Exception, attempt: int, cold_start_s: float = 90.0) -> float:
    """Backoff before retry number ``attempt`` (one-based): exponential, capped, jittered."""
    cfg = load_taxonomy().raw.get("llm_retry") or {}
    base = float(cfg.get("base_delay", 1.0))
    max_delay = float(cfg.get("max_delay", 30.0))
    jitter = float(cfg.get("jitter", 0.3))
    grow = 2 ** max(0, attempt - 1)
    if _status_code(exc) == 503:
        cap = float(cfg.get("modal_cold_start_max_delay", 240.0))
        delay = min(cap, cold_start_s * grow)
    elif isinstance(exc, RateLimitError) or _status_code(exc) == 429:
        rl_base = float(cfg.get("rate_limit_base_delay", 8.0))
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            rl_base = max(rl_base, retry_after)
        delay = min(max_delay, rl_base * grow)
    else:
        delay = min(max_delay, base * grow)
    return max(0.0, delay * (1 + random.uniform(-jitter, jitter)))


def _record_retry_event(exc: Exception, attempt: int, max_attempts: int, delay: float) -> None:
    """Add a ``mailroom.llm_retry`` event without message text, if recording.

    ``attempt`` is the one-based failed call number; ``max_attempts`` includes
    the initial call. ``delay`` is in seconds. Annotation errors are suppressed.
    """
    try:
        span = trace.get_current_span()
        if span.is_recording():
            attrs = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_type": type(exc).__name__,
                "retry_in_s": round(delay, 2),
            }
            code = _status_code(exc)
            if code is not None:
                attrs["status_code"] = code
            span.add_event("mailroom.llm_retry", attrs)
    except Exception:
        logger.debug("llm_retry_event_failed", exc_info=True)


def with_retry(fn: Callable[[], T], *, cold_start_s: float = 90, max_attempts: int = 4) -> T:
    """Return ``fn()``'s result, retrying transient failures with jittered backoff.

    ``max_attempts`` includes the initial call, which runs even when the limit
    is nonpositive. ``cold_start_s`` is the base delay in seconds for HTTP 503.
    Record a retry event and sleep before each retry. Nontransient errors and
    the last error at the attempt limit propagate; backoff configuration and
    sleep errors also propagate.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if not is_transient_error(exc) or attempt >= max_attempts:
                raise
            delay = retry_sleep_seconds(exc, attempt, cold_start_s)
            _record_retry_event(exc, attempt, max_attempts, delay)
            logger.warning(
                "llm_retry",
                attempt=attempt,
                max_attempts=max_attempts,
                error_type=type(exc).__name__,
                detail=str(exc)[:300],
                retry_in_s=round(delay, 2),
            )
            _sleep(max(0.0, delay))
