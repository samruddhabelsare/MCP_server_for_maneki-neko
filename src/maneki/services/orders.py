"""Order processing and lifecycle management service.

Handles:
  - Atomic confirmation of drafts into orders via Postgres RPC
  - Total recalculation against live DB menu prices at confirm time
  - Status lifecycle transitions and validation
  - Payment recording (mark_billed)
  - Historical order listing
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import (
    DraftNotFoundError,
    EmptyDraftError,
    InvalidStatusTransitionError,
    OrderNotFoundError,
    SessionNotFoundError,
)
from maneki.models import (
    ORDER_TRANSITIONS,
    Order,
    OrderItem,
)
from maneki.services.drafts import get_or_create_draft
from maneki.services.menu import get_menu_item
from maneki.services.sessions import get_session


def confirm_order(session_id: UUID | str) -> Order:
    """Atomically confirm an open draft into an order.

    Rules:
      1. Re-reads prices from the menu table and recalculates total.
      2. Rejects if draft is empty.
      3. Idempotent: if draft was already confirmed, returns the existing order.
      4. Executes via the confirm_draft Postgres RPC.
    """
    session = get_session(session_id)
    if not session:
        raise SessionNotFoundError(str(session_id))

    # First look for an already-confirmed draft (idempotency: second call after confirm)
    db = get_db()
    confirmed_res = (
        db.table("order_drafts")
        .select("*")
        .eq("session_id", str(session.id))
        .eq("status", "confirmed")
        .limit(1)
        .execute()
    )
    confirmed_rows = cast(list[dict[str, Any]], confirmed_res.data) if confirmed_res.data else []
    if confirmed_rows:
        existing_draft_raw = confirmed_rows[0]
        existing_order_id = existing_draft_raw.get("order_id")
        if existing_order_id:
            return get_order(existing_order_id)

    draft = get_or_create_draft(session.id, session.restaurant_id)

    if not draft.items:
        raise EmptyDraftError()

    # Re-read prices from live menu table
    recalculated_items: list[dict[str, Any]] = []
    total = 0.0

    for item in draft.items:
        menu_item = get_menu_item(session.restaurant_id, item.name)
        live_price = menu_item.price
        subtotal = round(live_price * item.qty, 2)
        total += subtotal
        recalculated_items.append(
            {
                "name": menu_item.name,
                "qty": item.qty,
                "price": live_price,
                "instructions": item.instructions,
            }
        )

    total_amount = round(total, 2)
    res = db.rpc(
        "confirm_draft",
        {
            "p_session_id": str(session.id),
            "p_items": recalculated_items,
            "p_total": total_amount,
        },
    ).execute()

    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        raise DraftNotFoundError()

    return _parse_order(rows[0])


def get_order(order_id: UUID | str) -> Order:
    """Retrieve an order by ID."""
    cfg = get_settings()
    db = get_db()
    res = db.table(cfg.orders_table).select("*").eq("id", str(order_id)).limit(1).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        raise OrderNotFoundError(str(order_id))

    return _parse_order(rows[0])


def get_order_status(order_id: UUID | str) -> dict[str, Any]:
    """Retrieve current status and summary for polling."""
    order = get_order(order_id)
    return {
        "order_id": str(order.id),
        "status": order.status,
        "table_number": order.table_number,
        "items": [it.model_dump() for it in order.items],
        "total_amount": order.total_amount,
        "payment_method": order.payment_method,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def update_order_status(order_id: UUID | str, new_status: str) -> Order:
    """Update order status following the strict lifecycle transitions."""
    order = get_order(order_id)
    current = order.status.lower()
    target = new_status.lower()

    allowed = ORDER_TRANSITIONS.get(current, [])
    if target not in allowed:
        raise InvalidStatusTransitionError(current=current, attempted=target, allowed=allowed)

    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.orders_table)
        .update({"status": target})
        .eq("id", str(order.id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    data = rows[0] if rows else {**order.model_dump(), "status": target}
    return _parse_order(data)


def mark_billed(order_id: UUID | str, payment_method: str = "cash") -> Order:
    """Transition an order to billed with recorded payment method."""
    order = get_order(order_id)
    current = order.status.lower()

    # Billed is allowed from delivered (or preparing/ready in manual overrides if allowed)
    allowed = ORDER_TRANSITIONS.get(current, [])
    if "billed" not in allowed and current != "billed":
        raise InvalidStatusTransitionError(current=current, attempted="billed", allowed=allowed)

    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.orders_table)
        .update({"status": "billed", "payment_method": payment_method})
        .eq("id", str(order.id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    data = rows[0] if rows else {**order.model_dump(), "status": "billed", "payment_method": payment_method}
    return _parse_order(data)


def list_customer_history(
    customer_id: UUID | str,
    restaurant_id: UUID | str,
    limit: int = 10,
) -> list[Order]:
    """Retrieve customer's past orders ordered by created_at descending."""
    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.orders_table)
        .select("*")
        .eq("customer_id", str(customer_id))
        .eq("restaurant_id", str(restaurant_id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    return [_parse_order(r) for r in rows]


def get_latest_active_order(
    session_id: UUID | str,
) -> Order | None:
    """Retrieve latest non-billed order for this session's customer/table."""
    session = get_session(session_id)
    cfg = get_settings()
    db = get_db()

    query = (
        db.table(cfg.orders_table)
        .select("*")
        .eq("restaurant_id", str(session.restaurant_id))
        .eq("table_number", session.table_number)
    )
    if session.customer_id:
        query = query.eq("customer_id", str(session.customer_id))

    res = query.order("created_at", desc=True).limit(5).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []

    for r in rows:
        order = _parse_order(r)
        if order.status not in ("billed", "cancelled"):
            return order

    return None


def _parse_order(raw: dict[str, Any]) -> Order:
    raw_items = raw.get("items") or []
    items: list[OrderItem] = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if isinstance(it, dict):
                items.append(
                    OrderItem(
                        name=str(it["name"]),
                        qty=int(it["qty"]),
                        price=float(it["price"]),
                        instructions=str(it.get("instructions") or ""),
                    )
                )

    return Order(
        id=UUID(str(raw["id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        customer_id=UUID(str(raw["customer_id"])) if raw.get("customer_id") else None,
        table_number=int(raw["table_number"]),
        items=items,
        total_amount=float(raw.get("total_amount") or 0.0),
        status=str(raw.get("status") or "pending"),
        payment_method=str(raw.get("payment_method") or "cash"),
        customer_phone=str(raw["customer_phone"]) if raw.get("customer_phone") else None,
        bot_id=UUID(str(raw["bot_id"])) if raw.get("bot_id") else None,
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )
