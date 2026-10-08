"""Customer management service.

Handles customer resolution, profile updates, phone normalization,
and past-order preferences/favorites extraction.

Phase 3: Customer profile context is cached in profile_cache.
Cache is invalidated after confirm_order for that customer.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from maneki.cache import profile_cache
from maneki.config import get_settings
from maneki.db import get_db
from maneki.models import Customer, SessionContext


def normalize_phone(raw: str | None) -> str | None:
    """Normalize phone numbers according to PRD Section 8 Rule 1.

    Rules:
      1. Strip all non-digit characters.
      2. If 12 digits starting with '91', drop the leading '91'.
      3. If 10 digits, format as '+91-XXXXX-XXXXX'.
      4. Otherwise return input unchanged.
    """
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    if len(digits) == 10:
        return f"+91-{digits[:5]}-{digits[5:]}"

    return raw


def get_customer(customer_id: UUID | str) -> Customer | None:
    """Fetch customer by ID."""
    cfg = get_settings()
    db = get_db()
    res = db.table(cfg.customers_table).select("*").eq("id", str(customer_id)).limit(1).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        return None

    return _parse_customer(rows[0])


def upsert_customer(
    restaurant_id: UUID | str,
    phone: str | None,
    name: str | None = None,
    preferences: list[str] | None = None,
) -> Customer:
    """Find customer by (restaurant_id, phone) or create if new.

    Increments visit_count on returning visits.
    """
    cfg = get_settings()
    db = get_db()
    norm_phone = normalize_phone(phone)

    if norm_phone:
        res = (
            db.table(cfg.customers_table)
            .select("*")
            .eq("restaurant_id", str(restaurant_id))
            .eq("phone", norm_phone)
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], res.data) if res.data else []
        if rows:
            existing = rows[0]
            new_visit = int(existing.get("visit_count") or 1) + 1
            updates: dict[str, Any] = {"visit_count": new_visit}
            if name:
                updates["name"] = name
            if preferences is not None:
                cur_prefs = set(cast(list[str], existing.get("preferences") or []))
                cur_prefs.update(preferences)
                updates["preferences"] = list(cur_prefs)

            db.table(cfg.customers_table).update(updates).eq("id", str(existing["id"])).execute()
            updated_data = {**existing, **updates}
            customer = _parse_customer(updated_data)
            # Invalidate profile cache on update
            profile_cache.invalidate(f"profile:{customer.id}:{restaurant_id}")
            return customer

    cid = uuid4()
    cust_name = name or "Customer"
    new_row: dict[str, Any] = {
        "id": str(cid),
        "restaurant_id": str(restaurant_id),
        "name": cust_name,
        "phone": norm_phone,
        "visit_count": 1,
        "preferences": preferences or [],
    }

    res = db.table(cfg.customers_table).insert(new_row).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    data = rows[0] if rows else new_row
    return _parse_customer(data)


def get_customer_context(session: SessionContext) -> dict[str, Any]:
    """Build context dict for customer MCP tool.

    For guests: minimal information.
    For registered customers: name, preferences, visit_count, top-5 favorite dishes.

    Phase 3: Result is cached in profile_cache.
    """
    if not session.customer_id:
        return {
            "guest": True,
            "name": "Guest",
            "preferences": [],
            "visit_count": 1,
            "top_favorites": [],
        }

    cache_key = f"profile:{session.customer_id}:{session.restaurant_id}"
    cfg = get_settings()
    cached = profile_cache.get(cache_key)
    if cached is not None:
        return cast(dict[str, Any], cached)

    customer = get_customer(session.customer_id)
    if not customer:
        return {
            "guest": True,
            "name": "Guest",
            "preferences": [],
            "visit_count": 1,
            "top_favorites": [],
        }

    db = get_db()
    orders_res = (
        db.table(cfg.orders_table)
        .select("items")
        .eq("restaurant_id", str(session.restaurant_id))
        .eq("customer_id", str(session.customer_id))
        .limit(20)
        .execute()
    )
    orders_rows = cast(list[dict[str, Any]], orders_res.data) if orders_res.data else []

    item_counts: Counter[str] = Counter()
    for o in orders_rows:
        raw_items = o.get("items")
        if isinstance(raw_items, list):
            for it in raw_items:
                if isinstance(it, dict) and "name" in it:
                    qty = int(it.get("qty", 1))
                    item_counts[str(it["name"])] += qty

    top_favorites = [name for name, _ in item_counts.most_common(5)]

    result: dict[str, Any] = {
        "guest": False,
        "name": customer.name,
        "preferences": customer.preferences,
        "visit_count": customer.visit_count,
        "top_favorites": top_favorites,
    }
    profile_cache.set(cache_key, result, ttl=cfg.profile_cache_ttl)
    return result


def invalidate_customer_profile(customer_id: UUID | str, restaurant_id: UUID | str) -> None:
    """Invalidate profile cache for a customer after confirm_order."""
    profile_cache.invalidate(f"profile:{customer_id}:{restaurant_id}")


def _parse_customer(raw: dict[str, Any]) -> Customer:
    prefs = raw.get("preferences") or []
    if isinstance(prefs, str):
        prefs = [p.strip() for p in prefs.strip("{}").split(",") if p.strip()]

    return Customer(
        id=UUID(str(raw["id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        name=str(raw["name"]),
        phone=str(raw["phone"]) if raw.get("phone") else None,
        visit_count=int(raw.get("visit_count") or 1),
        preferences=list(prefs),
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )
