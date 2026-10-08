"""Draft order management service.

Handles adding, modifying quantities, removing, clearing, and fetching draft orders.
Ensures single open draft per session, validates menu availability and canonical prices,
and caps instructions to 200 characters.

Phase 4: Mutating operations (add/set/remove/clear) call the draft_apply() Postgres
RPC for atomic read-then-write in a single round trip.  get_current_draft remains
a direct SELECT (no RPC needed).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from maneki.db import get_db
from maneki.errors import ItemUnavailableError, ValidationError
from maneki.models import Draft, DraftItem
from maneki.services.menu import get_menu_item


def get_or_create_draft(session_id: UUID | str, restaurant_id: UUID | str) -> Draft:
    """Retrieve the open draft for a session or create a new one."""
    sid = str(session_id)
    rid = str(restaurant_id)
    db = get_db()

    res = (
        db.table("order_drafts")
        .select("*")
        .eq("session_id", sid)
        .eq("status", "open")
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if rows:
        return _parse_draft(rows[0])

    draft_id = uuid4()
    new_row: dict[str, Any] = {
        "id": str(draft_id),
        "session_id": sid,
        "restaurant_id": rid,
        "items": [],
        "status": "open",
    }
    insert_res = db.table("order_drafts").insert(new_row).execute()
    insert_rows = cast(list[dict[str, Any]], insert_res.data) if insert_res.data else []
    data = insert_rows[0] if insert_rows else new_row
    return _parse_draft(data)


def get_current_draft(session_id: UUID | str, restaurant_id: UUID | str) -> Draft:
    """Retrieve current open draft, creating one if not present."""
    return get_or_create_draft(session_id, restaurant_id)


def add_item(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    name: str,
    qty: int = 1,
    instructions: str = "",
) -> Draft:
    """Add quantity of an item to the draft (via draft_apply RPC).

    Resolves item name canonicalized against the menu (LIVE lookup).
    Rejects unavailable items.
    Accumulates quantity if the item already exists in the draft.
    """
    if qty < 1:
        raise ValidationError("Quantity must be at least 1.")

    menu_item = get_menu_item(restaurant_id, name)
    if not menu_item.is_available:
        raise ItemUnavailableError(menu_item.name)

    clean_instructions = instructions[:200].strip()
    return _rpc_apply(
        session_id=session_id,
        restaurant_id=restaurant_id,
        op="add",
        name=menu_item.name,
        qty=qty,
        price=menu_item.price,
        instructions=clean_instructions,
    )


def remove_item(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    name: str,
) -> Draft:
    """Remove an item completely from the draft (via draft_apply RPC)."""
    # Attempt canonical name resolution; fall back to raw name if not found
    canonical_name = name
    try:
        menu_item = get_menu_item(restaurant_id, name)
        canonical_name = menu_item.name
    except Exception:
        pass

    return _rpc_apply(
        session_id=session_id,
        restaurant_id=restaurant_id,
        op="remove",
        name=canonical_name,
    )


def set_quantity(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    name: str,
    qty: int,
) -> Draft:
    """Set absolute quantity of an item in the draft (via draft_apply RPC).

    qty = 0 removes the item. Live menu lookup validates availability.
    """
    if qty < 0:
        raise ValidationError("Quantity cannot be negative.")

    if qty == 0:
        return remove_item(session_id, restaurant_id, name)

    menu_item = get_menu_item(restaurant_id, name)
    if not menu_item.is_available:
        raise ItemUnavailableError(menu_item.name)

    return _rpc_apply(
        session_id=session_id,
        restaurant_id=restaurant_id,
        op="set",
        name=menu_item.name,
        qty=qty,
        price=menu_item.price,
    )


def clear_draft(session_id: UUID | str, restaurant_id: UUID | str) -> Draft:
    """Empty all items from the draft (via draft_apply RPC)."""
    return _rpc_apply(
        session_id=session_id,
        restaurant_id=restaurant_id,
        op="clear",
    )


def format_draft(draft: Draft) -> dict[str, Any]:
    """Format draft into the standard response shape for MCP tools and REST."""
    lines: list[dict[str, Any]] = []
    for item in draft.items:
        lines.append(
            {
                "name": item.name,
                "qty": item.qty,
                "price": item.price,
                "subtotal": round(item.price * item.qty, 2),
                "instructions": item.instructions,
            }
        )

    return {
        "draft_id": str(draft.id),
        "items": lines,
        "total": round(sum(line["subtotal"] for line in lines), 2),
        "item_count": sum(line["qty"] for line in lines),
    }


# ── Phase 4 RPC helper ────────────────────────────────────────────────────────

def _rpc_apply(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    op: str,
    name: str = "",
    qty: int = 1,
    price: float = 0.0,
    instructions: str = "",
) -> Draft:
    """Call draft_apply() Postgres RPC and return the resulting Draft.

    Falls back to Python-level read-then-write if the RPC returns no rows
    (e.g. in test environments without the function installed).
    """
    db = get_db()
    params: dict[str, Any] = {
        "p_session_id": str(session_id),
        "p_restaurant_id": str(restaurant_id),
        "p_op": op,
        "p_name": name,
        "p_qty": qty,
        "p_price": price,
        "p_instructions": instructions,
    }

    try:
        res = db.rpc("draft_apply", params).execute()
        rows = cast(list[dict[str, Any]], res.data) if res.data else []
        if rows:
            return _parse_draft(rows[0])
    except Exception:
        pass

    # Fallback: Python-level implementation (used in tests / pre-migration)
    return _python_apply(
        session_id=session_id,
        restaurant_id=restaurant_id,
        op=op,
        name=name,
        qty=qty,
        price=price,
        instructions=instructions,
    )


def _python_apply(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    op: str,
    name: str = "",
    qty: int = 1,
    price: float = 0.0,
    instructions: str = "",
) -> Draft:
    """Python-level fallback for draft_apply (used in tests)."""
    draft = get_or_create_draft(session_id, restaurant_id)
    items = list(draft.items)
    name_lc = name.strip().lower()

    if op == "clear":
        items = []

    elif op == "remove":
        items = [it for it in items if it.name.lower() != name_lc]

    elif op == "add":
        found = False
        for i, it in enumerate(items):
            if it.name.lower() == name_lc:
                new_inst = instructions or it.instructions
                items[i] = DraftItem(
                    name=name,
                    qty=it.qty + qty,
                    price=price,
                    instructions=new_inst,
                )
                found = True
                break
        if not found:
            items.append(DraftItem(name=name, qty=qty, price=price, instructions=instructions))

    elif op == "set":
        if qty == 0:
            items = [it for it in items if it.name.lower() != name_lc]
        else:
            found = False
            for i, it in enumerate(items):
                if it.name.lower() == name_lc:
                    items[i] = DraftItem(name=name, qty=qty, price=price, instructions=it.instructions)
                    found = True
                    break
            if not found:
                items.append(DraftItem(name=name, qty=qty, price=price, instructions=instructions))

    return _save_draft_items(draft.id, items, draft)


def _save_draft_items(draft_id: UUID, items: list[DraftItem], original_draft: Draft) -> Draft:
    db = get_db()
    items_json = [item.model_dump() for item in items]
    res = (
        db.table("order_drafts")
        .update({"items": items_json})
        .eq("id", str(draft_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if rows:
        return _parse_draft(rows[0])

    return Draft(
        id=original_draft.id,
        session_id=original_draft.session_id,
        restaurant_id=original_draft.restaurant_id,
        items=items,
        status="open",
    )


def _parse_draft(raw: dict[str, Any]) -> Draft:
    raw_items = raw.get("items") or []
    items: list[DraftItem] = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if isinstance(it, dict):
                items.append(
                    DraftItem(
                        name=str(it["name"]),
                        qty=int(it["qty"]),
                        price=float(it["price"]),
                        instructions=str(it.get("instructions") or "")[:200],
                    )
                )

    return Draft(
        id=UUID(str(raw["id"])),
        session_id=UUID(str(raw["session_id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        items=items,
        status=str(raw.get("status") or "open"),
        order_id=UUID(str(raw["order_id"])) if raw.get("order_id") else None,
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
        updated_at=datetime.fromisoformat(str(raw["updated_at"])) if raw.get("updated_at") else None,
    )
