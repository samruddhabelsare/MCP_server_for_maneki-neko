"""Rate limiting configuration using slowapi.

Handles:
  1. Proxy-aware real client IP extraction via X-Forwarded-For (prevents Render proxy IP sharing).
  2. Session-keyed rate limiting for /sessions/{id}/chat (prevents restaurant Wi-Fi NAT shared buckets).
  3. Global 200/min per-IP rate limiting across the entire server.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Request
from slowapi import Limiter

logger = logging.getLogger("maneki.rate_limit")


def get_real_ip(request: Request) -> str:
    """Extract real client IP taking X-Forwarded-For into account for Render/reverse proxies.

    On Render or other reverse proxies, request.client.host is the proxy itself.
    The leftmost IP in X-Forwarded-For is the true client address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Leftmost entry is the originating client
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def get_chat_rate_limit_key(request: Request) -> str:
    """Derive rate-limit key for chat turns: session ID first, falling back to IP.

    Because restaurant patrons frequently connect through a single Wi-Fi NAT
    router, keying on IP alone would cause all tables in the restaurant to share
    a single 30 req/min bucket.

    Key resolution:
      1. Session ID extracted from Authorization Bearer token (<sid>.<exp>.<sig>)
      2. Session ID extracted from URL path parameter (:id)
      3. Real client IP as final fallback
    """
    auth = request.headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        raw_token = auth[7:].strip()
        parts = raw_token.split(".")
        if len(parts) >= 1:
            try:
                sid = UUID(parts[0])
                return f"sess:{sid}"
            except ValueError:
                pass

    path_id = request.path_params.get("id")
    if path_id:
        try:
            sid = UUID(str(path_id))
            return f"sess:{sid}"
        except ValueError:
            return f"sess:{path_id}"

    return f"ip:{get_real_ip(request)}"


# Global default limiter uses proxy-aware client IP
limiter = Limiter(key_func=get_real_ip, default_limits=["200/minute"])
