"""Authentication and session-binding helpers for MCP tools and REST endpoints."""
from __future__ import annotations

import secrets
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import Context

from maneki.config import get_settings
from maneki.errors import ForbiddenError, SessionNotFoundError, UnauthorizedError
from maneki.models import SessionContext
from maneki.services.sessions import get_session

# ContextVar for in-process binding (orchestrator, tests, ASGI middleware)
current_session_ctx: ContextVar[SessionContext | None] = ContextVar("current_session_ctx", default=None)
current_admin_auth: ContextVar[bool] = ContextVar("current_admin_auth", default=False)


def require_session(ctx: Context[Any, Any] | None = None) -> SessionContext:
    """Resolve and validate the active session for customer MCP tools.

    Checks:
      1. ContextVar override (set in tests or orchestrator).
      2. HTTP request headers (X-Session-Id and Authorization: Bearer <INTERNAL_MCP_TOKEN>).

    Raises:
      UnauthorizedError: If the internal MCP token is missing or invalid.
      SessionNotFoundError: If X-Session-Id is missing or not in database.
      SessionExpiredError: If the session has expired.
    """
    ctx_override = current_session_ctx.get()
    if ctx_override is not None:
        # Re-check that session is still valid in DB
        session = get_session(ctx_override.session_id)
        return SessionContext(
            session_id=session.id,
            restaurant_id=session.restaurant_id,
            table_number=session.table_number,
            customer_id=session.customer_id,
            character=session.character,
        )

    if ctx is not None:
        req = getattr(getattr(ctx, "request_context", None), "request", None)
        if req is not None and hasattr(req, "headers"):
            headers = req.headers
            auth_header = headers.get("Authorization", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()

            cfg = get_settings()
            if not token or not secrets.compare_digest(token, cfg.internal_mcp_token):
                raise UnauthorizedError("Invalid or missing internal MCP token")

            session_id_raw = headers.get("X-Session-Id")
            if not session_id_raw:
                raise SessionNotFoundError()

            session = get_session(session_id_raw)
            return SessionContext(
                session_id=session.id,
                restaurant_id=session.restaurant_id,
                table_number=session.table_number,
                customer_id=session.customer_id,
                character=session.character,
            )

    raise UnauthorizedError("Session context required")


def require_admin(ctx: Context[Any, Any] | None = None, admin_key: str | None = None) -> None:
    """Validate admin authorization for admin MCP tools.

    Checks:
      1. Admin contextvar.
      2. Explicit admin_key argument.
      3. HTTP header X-Admin-Key on ctx.request_context.request.

    Raises:
      ForbiddenError: If key does not match ADMIN_API_KEY.
    """
    if current_admin_auth.get():
        return

    cfg = get_settings()
    key = admin_key

    if not key and ctx is not None:
        req = getattr(getattr(ctx, "request_context", None), "request", None)
        if req is not None and hasattr(req, "headers"):
            key = req.headers.get("X-Admin-Key")

    if not key or not secrets.compare_digest(key, cfg.admin_api_key):
        raise ForbiddenError("Invalid or missing admin key")
