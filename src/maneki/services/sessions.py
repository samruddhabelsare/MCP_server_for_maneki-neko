"""Session management service.

Handles resolving, validating, and creating sessions.
Enforces expiration (SESSION_TTL_HOURS) and multi-restaurant scoping.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

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

    return session


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

    return Session(
        id=UUID(str(data["id"])),
        restaurant_id=UUID(str(data["restaurant_id"])),
        table_number=int(data["table_number"]),
        customer_id=UUID(str(data["customer_id"])) if data.get("customer_id") else None,
        character=str(data.get("character") or character),
        expires_at=datetime.fromisoformat(str(data["expires_at"])),
        created_at=datetime.fromisoformat(str(data["created_at"])) if data.get("created_at") else None,
    )
