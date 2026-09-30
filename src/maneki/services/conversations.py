"""Conversation persistence service.

Handles:
  - Getting or creating a conversation for a session (one per session)
  - Saving user, assistant, and tool messages
  - Loading message history for chat restoration on refresh / device switch
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from maneki.db import get_db
from maneki.models import Conversation, Message


def get_or_create_conversation(
    session_id: UUID | str,
    restaurant_id: UUID | str,
) -> Conversation:
    """Return the conversation for this session, creating it if absent."""
    sid = str(session_id)
    rid = str(restaurant_id)
    db = get_db()

    res = (
        db.table("conversations")
        .select("*")
        .eq("session_id", sid)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if rows:
        return _parse_conversation(rows[0])

    new_row: dict[str, Any] = {
        "id": str(uuid4()),
        "session_id": sid,
        "restaurant_id": rid,
    }
    insert_res = db.table("conversations").insert(new_row).execute()
    insert_rows = cast(list[dict[str, Any]], insert_res.data) if insert_res.data else []
    return _parse_conversation(insert_rows[0] if insert_rows else new_row)


def save_message(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    role: str,
    content: str,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
) -> Message:
    """Persist a single message to the conversation for this session.

    Creates the conversation row if it does not yet exist.
    Role must be one of: 'user', 'assistant', 'tool'.
    """
    conv = get_or_create_conversation(session_id, restaurant_id)
    db = get_db()

    new_row: dict[str, Any] = {
        "id": str(uuid4()),
        "conversation_id": str(conv.id),
        "role": role,
        "content": content[:8000],  # cap content length
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }
    insert_res = db.table("messages").insert(new_row).execute()
    insert_rows = cast(list[dict[str, Any]], insert_res.data) if insert_res.data else []
    return _parse_message(insert_rows[0] if insert_rows else new_row)


def load_messages(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    limit: int = 50,
) -> list[Message]:
    """Load the last `limit` messages for this session, oldest first.

    Returns an empty list for sessions with no conversation yet.
    """
    conv_res = (
        get_db().table("conversations")
        .select("id")
        .eq("session_id", str(session_id))
        .limit(1)
        .execute()
    )
    conv_rows = cast(list[dict[str, Any]], conv_res.data) if conv_res.data else []
    if not conv_rows:
        return []

    conv_id = conv_rows[0]["id"]
    db = get_db()
    res = (
        db.table("messages")
        .select("*")
        .eq("conversation_id", str(conv_id))
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    return [_parse_message(r) for r in rows]


def _parse_conversation(raw: dict[str, Any]) -> Conversation:
    return Conversation(
        id=UUID(str(raw["id"])),
        session_id=UUID(str(raw["session_id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )


def _parse_message(raw: dict[str, Any]) -> Message:
    return Message(
        id=UUID(str(raw["id"])),
        conversation_id=UUID(str(raw["conversation_id"])),
        role=str(raw["role"]),
        content=str(raw["content"]),
        tool_name=str(raw["tool_name"]) if raw.get("tool_name") else None,
        tool_call_id=str(raw["tool_call_id"]) if raw.get("tool_call_id") else None,
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )
