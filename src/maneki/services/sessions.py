"""Session management service.

Handles resolving, validating, and creating sessions.
Enforces expiration (SESSION_TTL_HOURS) and multi-restaurant scoping.

Phase 3: Session rows are cached in session_cache.
Each hit re-checks expires_at against current time (no stale expiry misses).
Cache is invalidated on confirm/expiry.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from maneki.cache import session_cache
from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import SessionExpiredError, SessionNotFoundError
from maneki.models import Session


def get_session(session_id: UUID | str) -> Session:
    """Retrieve an active, unexpired session by ID.

    Raises:
        SessionNotFoundError: If session is not in the database.
        SessionExpiredError: If session has expired.
    """
    sid = str(session_id)
    cfg = get_settings()

    # Check cache first
    cache_key = f"session:{sid}"
    cached = session_cache.get(cache_key)
    if cached is not None:
        session = cast(Session, cached)
        # Always re-verify expires_at on cache hit
        now = datetime.now(UTC)
        exp = session.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp < now:
            session_cache.invalidate(cache_key)
            raise SessionExpiredError()
        return session

    # Cache miss — hit DB
    db = get_db()
    res = db.table("sessions").select("*").eq("id", sid).limit(1).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        raise SessionNotFoundError(sid)

    raw = rows[0]
    session = Session(
        id=UUID(str(raw["id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        table_number=int(raw["table_number"]),
        customer_id=UUID(str(raw["customer_id"])) if raw.get("customer_id") else None,
        character=str(raw.get("character") or "neko"),
        expires_at=datetime.fromisoformat(str(raw["expires_at"])),
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )

    now = datetime.now(UTC)
    exp = session.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)

    if exp < now:
        raise SessionExpiredError()

    session_cache.set(cache_key, session, ttl=cfg.session_cache_ttl)
    return session


def invalidate_session_cache(session_id: UUID | str) -> None:
    """Invalidate a session from the cache (call after confirm/expiry)."""
    session_cache.invalidate(f"session:{session_id}")


def create_session(
    restaurant_id: UUID | str,
    table_number: int,
    customer_id: UUID | str | None = None,
    character: str = "neko",
) -> Session:
    """Create a new session with an expiration timestamp."""
    cfg = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=cfg.session_ttl_hours)
    sid = uuid4()

    row: dict[str, Any] = {
        "id": str(sid),
        "restaurant_id": str(restaurant_id),
        "table_number": table_number,
        "customer_id": str(customer_id) if customer_id else None,
        "character": character,
        "expires_at": expires_at.isoformat(),
        "created_at": now.isoformat(),
    }

    db = get_db()
    res = db.table("sessions").insert(row).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    data = rows[0] if rows else row

    session = Session(
        id=UUID(str(data["id"])),
        restaurant_id=UUID(str(data["restaurant_id"])),
        table_number=int(data["table_number"]),
        customer_id=UUID(str(data["customer_id"])) if data.get("customer_id") else None,
        character=str(data.get("character") or character),
        expires_at=datetime.fromisoformat(str(data["expires_at"])),
        created_at=datetime.fromisoformat(str(data["created_at"])) if data.get("created_at") else None,
    )
    # Prime the cache immediately after creation
    session_cache.set(f"session:{session.id}", session, ttl=cfg.session_cache_ttl)
    return session
