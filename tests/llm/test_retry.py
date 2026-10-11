"""Unit tests for the LLM transport-retry backoff policy.

``is_transient_error`` and ``retry_sleep_seconds`` decide whether a failed LLM
call is retried and how long to wait. The taxonomy's ``llm_retry`` block
(``base_delay``/``rate_limit_base_delay``/``max_delay``/``modal_cold_start_*``)
is the contract; these tests pin each branch so a regression to silent (no
retry, or a wrong ladder) fails here.
"""

from __future__ import annotations

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)

from mailroom_reloaded.llm.retry import (
    _retry_after_seconds,
    is_transient_error,
    retry_sleep_seconds,
)

_URL = "http://endpoint.example.test/v1/chat/completions"


def _status_exc(cls, status: int, message: str = "boom", headers: dict | None = None):
    """Build an OpenAI status error carrying a real ``httpx.Response``."""
    request = httpx.Request("POST", _URL)
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls(message, response=response, body=None)


# --------------------------------------------------------------- is_transient_error


def test_is_transient_error_connection_timeout_and_rate_limit():
    request = httpx.Request("POST", _URL)
    assert is_transient_error(APIConnectionError(request=request)) is True
    assert is_transient_error(APITimeoutError(request=request)) is True
    assert is_transient_error(_status_exc(RateLimitError, 429)) is True


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_is_transient_error_retryable_statuses(status):
    assert is_transient_error(_status_exc(APIStatusError, status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_is_transient_error_non_retryable_statuses(status):
    assert is_transient_error(_status_exc(APIStatusError, status)) is False


def test_is_transient_error_json_mode_400_is_retryable():
    """Alibaba/Qwen's intermittent json_object 400 succeeds on a plain retry."""
    marker = "response_format of type json_object must contain the word 'json'"
    assert is_transient_error(_status_exc(BadRequestError, 400, message=marker)) is True
    assert is_transient_error(_status_exc(BadRequestError, 400, message="other 400")) is False


def test_is_transient_error_plain_exception_is_not_retryable():
    assert is_transient_error(ValueError("nope")) is False


# --------------------------------------------------------------- _retry_after_seconds


def test_retry_after_seconds_reads_numeric_header_case_insensitively():
    assert _retry_after_seconds(_status_exc(RateLimitError, 429, headers={"Retry-After": "12"})) == 12.0
    assert _retry_after_seconds(_status_exc(RateLimitError, 429, headers={"retry-after": "2.5"})) == 2.5


def test_retry_after_seconds_clamps_negative_and_rejects_non_numeric():
    assert _retry_after_seconds(_status_exc(RateLimitError, 429, headers={"Retry-After": "-3"})) == 0.0
    # an HTTP-date is not a number: return None (fall back to the base ladder)
    assert _retry_after_seconds(
        _status_exc(RateLimitError, 429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    ) is None


def test_retry_after_seconds_none_when_header_absent():
    assert _retry_after_seconds(_status_exc(RateLimitError, 429)) is None
    assert _retry_after_seconds(ValueError("no response attr")) is None


# --------------------------------------------------------------- retry_sleep_seconds


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    """Zero the jitter so the ladder is deterministic."""
    from mailroom_reloaded.llm import retry

    monkeypatch.setattr(retry.random, "uniform", lambda _a, _b: 0.0)


def test_503_backs_off_on_the_cold_start_ladder_and_caps():
    exc = _status_exc(APIStatusError, 503)
    # cold_start_s default 90; grow = 2**(attempt-1); capped at modal_cold_start_max_delay (240)
    assert retry_sleep_seconds(exc, attempt=1) == 90.0
    assert retry_sleep_seconds(exc, attempt=2) == 180.0
    assert retry_sleep_seconds(exc, attempt=3) == 240.0  # min(240, 90*4)
    assert retry_sleep_seconds(exc, attempt=8) == 240.0  # stays capped


def test_429_honours_retry_after_over_the_base_delay():
    # no header -> rate_limit_base_delay (8.0) * grow
    assert retry_sleep_seconds(_status_exc(RateLimitError, 429), attempt=1) == 8.0
    # Retry-After above the base raises the ladder
    with_header = _status_exc(RateLimitError, 429, headers={"Retry-After": "12"})
    assert retry_sleep_seconds(with_header, attempt=1) == 12.0
    # Retry-After below the base does not lower it
    assert retry_sleep_seconds(_status_exc(RateLimitError, 429, headers={"Retry-After": "2"}), attempt=1) == 8.0


def test_generic_retryable_uses_base_delay_and_caps_at_max_delay():
    exc = _status_exc(APIStatusError, 500)
    assert retry_sleep_seconds(exc, attempt=1) == 1.0  # base_delay
    assert retry_sleep_seconds(exc, attempt=3) == 4.0  # 1 * 2**2
    assert retry_sleep_seconds(exc, attempt=10) == 60.0  # capped at max_delay
