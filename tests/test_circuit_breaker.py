"""Tests for AsyncCircuitBreaker and mid-stream failure tripping in orchestrator."""
import asyncio
import json
import time
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
import respx

from maneki.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerError
from maneki.config import get_settings
from maneki.orchestrator import _nim_breaker, stream_orchestrator_chat


@pytest.fixture(autouse=True)
def reset_breaker() -> None:
    """Ensure global circuit breaker is reset before and after each test."""
    _nim_breaker.reset()
    yield
    _nim_breaker.reset()


@pytest.fixture
def test_data(fake_db: Any) -> dict[str, Any]:
    """Seed minimal test data for circuit breaker tests."""
    rid = uuid4()
    cid = uuid4()
    sid = uuid4()

    fake_db.seed("restaurants", [{"id": str(rid), "name": "Neko Ramen Shibuya"}])
    fake_db.seed(
        "sessions",
        [
            {
                "id": str(sid),
                "restaurant_id": str(rid),
                "table_number": 1,
                "customer_id": str(cid),
                "character": "neko",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ],
    )
    return {"restaurant_id": rid, "session_id": sid, "customer_id": cid}


def test_circuit_breaker_basic_unit() -> None:
    cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=0.1, name="unit_test")
    assert not cb.is_open()

    # 4xx HTTP error should not count as failure
    resp_400 = httpx.Response(400, request=httpx.Request("POST", "https://api.nvidia.com"))
    cb.record_failure(httpx.HTTPStatusError("Bad request", request=resp_400.request, response=resp_400))
    assert cb.failure_count == 0
    assert not cb.is_open()

    # CancelledError should not count
    cb.record_failure(asyncio.CancelledError("Client disconnected"))
    assert cb.failure_count == 0
    assert not cb.is_open()

    # 5xx HTTP error counts
    resp_500 = httpx.Response(500, request=httpx.Request("POST", "https://api.nvidia.com"))
    cb.record_failure(httpx.HTTPStatusError("Server error", request=resp_500.request, response=resp_500))
    assert cb.failure_count == 1

    # Network timeouts count
    cb.record_failure(httpx.ConnectTimeout("Timed out"))
    assert cb.failure_count == 2
    assert not cb.is_open()

    # 3rd failure trips the breaker to OPEN
    cb.record_failure(httpx.ReadTimeout("Read timed out"))
    assert cb.failure_count == 3
    assert cb.is_open()
    with pytest.raises(CircuitBreakerError):
        cb.check_gate()

    # After reset_timeout, transitions to HALF_OPEN
    time.sleep(0.12)
    assert not cb.is_open()
    assert cb.state == "HALF_OPEN"

    # Success closes the breaker
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.failure_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_circuit_breaker_mid_stream_failures_trip_orchestrator(
    test_data: dict[str, Any],
) -> None:
    """Mock 5 consecutive NIM failures mid-stream and assert 6th request fast-fails."""
    cfg = get_settings()
    sid = test_data["session_id"]
    rid = test_data["restaurant_id"]

    # Stream generator that yields one valid token and then abruptly drops/errors mid-stream
    def make_failing_stream():
        async def stream():
            yield b'data: {"choices": [{"delta": {"content": "Starting response..."}}]}\n\n'
            raise httpx.RemoteProtocolError("Connection broken mid-stream")
        return stream()

    # Mock the endpoint to fail mid-stream for 5 consecutive calls
    respx.post(cfg.nvidia_endpoint).mock(
        side_effect=[httpx.Response(200, content=make_failing_stream()) for _ in range(5)]
    )

    # Execute 5 consecutive calls that fail mid-stream
    for i in range(5):
        events = []
        async for chunk in stream_orchestrator_chat(
            session_id=sid,
            restaurant_id=rid,
            table_number=1,
            customer_id=None,
            character="neko",
            user_message=f"Turn {i}",
        ):
            events.append(chunk)

        combined = "".join(events)
        assert "event: token" in combined  # received initial token piece
        assert "event: error" in combined  # failed mid-stream
        assert "AI stream interrupted" in combined or "llm_error" in combined

    # Breaker should now be in OPEN state
    assert _nim_breaker.is_open()
    assert _nim_breaker.failure_count >= 5

    # 6th request: Must fast-fail immediately with llm_unavailable WITHOUT contacting endpoint
    t_start = time.monotonic()
    t6_events = []
    async for chunk in stream_orchestrator_chat(
        session_id=sid,
        restaurant_id=rid,
        table_number=1,
        customer_id=None,
        character="neko",
        user_message="6th turn while circuit is open",
    ):
        t6_events.append(chunk)

    t6_combined = "".join(t6_events)
    elapsed = time.monotonic() - t_start

    # Assert fast-fail response
    assert "event: error" in t6_combined
    assert "llm_unavailable" in t6_combined
    assert "temporarily unavailable" in t6_combined
    # Fast fail should happen in < 100ms (no upstream network wait)
    assert elapsed < 0.2


@respx.mock
@pytest.mark.asyncio
async def test_circuit_breaker_4xx_does_not_trip_orchestrator(
    test_data: dict[str, Any],
) -> None:
    """HTTP 4xx responses (e.g. 400 Bad Request) must NOT count as circuit failures."""
    cfg = get_settings()
    sid = test_data["session_id"]
    rid = test_data["restaurant_id"]

    # Mock 5 consecutive 400 Bad Request responses
    respx.post(cfg.nvidia_endpoint).mock(
        return_value=httpx.Response(400, json={"error": "Bad Request: invalid token length"})
    )

    for i in range(5):
        events = []
        async for chunk in stream_orchestrator_chat(
            session_id=sid,
            restaurant_id=rid,
            table_number=1,
            customer_id=None,
            character="neko",
            user_message=f"Turn {i}",
        ):
            events.append(chunk)

        combined = "".join(events)
        assert "event: error" in combined
        assert "HTTP error" in combined or "llm_error" in combined

    # Breaker must still be CLOSED with 0 failures
    assert not _nim_breaker.is_open()
    assert _nim_breaker.failure_count == 0
    assert _nim_breaker.state == "CLOSED"

