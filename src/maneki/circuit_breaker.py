"""Async-native circuit breaker for streaming LLM calls.

Designed specifically for async generator and streaming HTTP calls where
failures occur during iteration (not just during function invocation).

Rules:
  - Consecutive qualifying failures (>= fail_max) trip the breaker to OPEN.
  - Once OPEN, subsequent requests fast-fail immediately without network calls.
  - After reset_timeout seconds, moves to HALF_OPEN to allow a single trial call.
  - 4xx HTTP responses (client errors) do NOT count as circuit failures.
  - asyncio.CancelledError (client disconnects) do NOT count as circuit failures.
  - Qualifying failures include 5xx server errors, connection timeouts, and network drops.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("maneki.circuit_breaker")


class CircuitBreakerError(Exception):
    """Raised when an operation is rejected because the circuit is OPEN."""


class AsyncCircuitBreaker:
    """Async circuit breaker with streaming failure detection and half-open state."""

    def __init__(
        self,
        fail_max: int = 5,
        reset_timeout: float = 60.0,
        name: str = "nim",
    ) -> None:
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.name = name
        self.failure_count = 0
        self.state = "CLOSED"  # "CLOSED", "OPEN", "HALF_OPEN"
        self.last_failure_time: float | None = None

    def is_open(self) -> bool:
        """Check if circuit is OPEN, handling transition to HALF_OPEN on timeout expiration."""
        if self.state == "CLOSED":
            return False

        if self.state == "OPEN":
            if (
                self.last_failure_time is not None
                and (time.monotonic() - self.last_failure_time) >= self.reset_timeout
            ):
                self.state = "HALF_OPEN"
                logger.info("CircuitBreaker '%s' entering HALF_OPEN probe state", self.name)
                return False
            return True

        # HALF_OPEN permits one trial request
        return False

    def check_gate(self) -> None:
        """Raise CircuitBreakerError if the circuit is currently open."""
        if self.is_open():
            raise CircuitBreakerError(
                f"Circuit breaker '{self.name}' is OPEN — fast-failing request."
            )

    def record_success(self) -> None:
        """Record a successful call/stream completion and close the circuit."""
        if self.state != "CLOSED":
            logger.info("CircuitBreaker '%s' recovered to CLOSED state", self.name)
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self, exc: Exception | None = None) -> None:
        """Record a qualifying failure.

        Filters out:
          - asyncio.CancelledError (client disconnected before stream completed)
          - 4xx HTTP status errors (client payload error, not provider failure)
        """
        if isinstance(exc, asyncio.CancelledError):
            return

        if isinstance(exc, httpx.HTTPStatusError):
            if 400 <= exc.response.status_code < 500:
                # 4xx is a client-side or semantic error, not an upstream infrastructure outage
                return

        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == "HALF_OPEN" or self.failure_count >= self.fail_max:
            self.state = "OPEN"
            logger.error(
                "CircuitBreaker '%s' TRIPPED to OPEN state (%d consecutive failures): %r",
                self.name,
                self.failure_count,
                exc,
            )

    def reset(self) -> None:
        """Reset breaker back to initial CLOSED state (for tests and administrative reset)."""
        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None
