"""Recommendation service.

Produces a ranked list of menu item suggestions for a customer session.

Ranking logic (higher score = better rank):
  1. Items the customer has ordered before get +2 per past order appearance.
  2. Items matching a customer preference keyword get +1 per match.
  3. Items with popularity score (count across all recent orders for that
     restaurant) get a fractional bonus so they break ties.

Each candidate includes a non-empty `reasons` list explaining why it was
suggested (e.g. "You ordered this before", "Popular this week").

Phase 3: Popularity scores per restaurant are cached in popularity_cache.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, cast
from uuid import UUID

from maneki.cache import popularity_cache
from maneki.config import get_settings
from maneki.db import get_db
from maneki.models import RecommendedItem, SessionContext
from maneki.services.menu import load_menu


def _load_popularity(restaurant_id: UUID | str) -> Counter[str]:
    """Load (cached) item popularity counter for a restaurant."""
    rid = str(restaurant_id)
    cfg = get_settings()
    cache_key = f"popularity:{rid}"
    cached = popularity_cache.get(cache_key)
    if cached is not None:
        return cast(Counter[str], cached)

    db = get_db()
    recent_orders_res = (
        db.table(cfg.orders_table)
        .select("items")
        .eq("restaurant_id", rid)
        .limit(100)
        .execute()
    )
    all_order_rows = cast(list[dict[str, Any]], recent_orders_res.data) if recent_orders_res.data else []
    popularity: Counter[str] = Counter()
    for o in all_order_rows:
        raw_items = o.get("items") or []
        if isinstance(raw_items, list):
            for it in raw_items:
                if isinstance(it, dict) and "name" in it:
                    popularity[str(it["name"]).lower()] += 1

    popularity_cache.set(cache_key, popularity, ttl=cfg.popularity_cache_ttl)
    return popularity


def recommend_dishes(
    session: SessionContext | None = None,
    restaurant_id: UUID | str | None = None,
    customer_id: UUID | str | None = None,
    veg_only: bool = False,
    exclude_spicy: bool = False,
    limit: int = 6,
) -> list[RecommendedItem]:
    """Return ranked recommendations for the session's customer/table.

    Args:
        session:       Resolved session context (optional if restaurant_id passed).
        restaurant_id: Restaurant UUID.
        customer_id:   Customer UUID, or None for guests.
        veg_only:      If True, exclude non-vegetarian items.
        exclude_spicy: If True, exclude spicy items.
        limit:         Maximum number of results (1–20).

    Returns:
        Up to `limit` RecommendedItem objects, each with a non-empty `reasons`
        list. Available items only.
    """
    rid: UUID | str
    cid: UUID | str | None
    if isinstance(session, SessionContext):
        rid = session.restaurant_id
        cid = session.customer_id
    elif session is not None:
        rid = cast(UUID | str, session)
        cid = customer_id
    elif restaurant_id is not None:
        rid = restaurant_id
        cid = customer_id
    else:
        raise ValueError("Either session or restaurant_id must be provided")

    limit = max(1, min(limit, 20))
    cfg = get_settings()
    db = get_db()

    # ── 1. Load available menu items (apply dietary filters early) ─────────
    available = load_menu(rid, include_unavailable=False)
    candidates = [
        item for item in available
        if (not veg_only or item.is_veg)
        and (not exclude_spicy or not item.is_spicy)
    ]
    if not candidates:
        return []

    # ── 2. Popularity (cached) ─────────────────────────────────────────────
    popularity = _load_popularity(rid)

    # ── 3. Customer history: past items ordered by this customer ──────────
    customer_history: Counter[str] = Counter()
    if cid:
        cust_orders_res = (
            db.table(cfg.orders_table)
            .select("items")
            .eq("restaurant_id", str(rid))
            .eq("customer_id", str(cid))
            .limit(20)
            .execute()
        )
        cust_rows = cast(list[dict[str, Any]], cust_orders_res.data) if cust_orders_res.data else []
        for o in cust_rows:
            raw_items = o.get("items") or []
            if isinstance(raw_items, list):
                for it in raw_items:
                    if isinstance(it, dict) and "name" in it:
                        customer_history[str(it["name"]).lower()] += 1

    # ── 4. Customer preferences (keyword matching) ─────────────────────────
    customer_prefs: list[str] = []
    if cid:
        pref_res = (
            db.table(cfg.customers_table)
            .select("preferences")
            .eq("id", str(cid))
            .limit(1)
            .execute()
        )
        pref_rows = cast(list[dict[str, Any]], pref_res.data) if pref_res.data else []
        if pref_rows:
            raw_prefs = pref_rows[0].get("preferences") or []
            if isinstance(raw_prefs, list):
                customer_prefs = [str(p).lower() for p in raw_prefs]
            elif isinstance(raw_prefs, str):
                customer_prefs = [p.strip().lower() for p in raw_prefs.strip("{}").split(",") if p.strip()]

    # ── 5. Score and build results ─────────────────────────────────────────
    scored: list[tuple[float, RecommendedItem]] = []
    total_orders = sum(popularity.values()) or 1  # avoid div-by-zero

    for item in candidates:
        name_lower = item.name.lower()
        score = 0.0
        reasons: list[str] = []

        # Past orders by this customer
        if customer_history[name_lower] > 0:
            score += customer_history[name_lower] * 2
            reasons.append("You've ordered this before")

        # Preference keyword match
        for pref in customer_prefs:
            if pref in name_lower or pref in item.category.lower():
                score += 1.0
                reasons.append(f"Matches your preference: {pref}")
                break  # one preference match per item is enough

        # Popularity bonus (fractional, 0–1)
        pop_count = popularity[name_lower]
        if pop_count > 0:
            pop_bonus = pop_count / total_orders
            score += pop_bonus
            if "You've ordered this before" not in reasons:
                reasons.append("Popular with other guests")

        # Guarantee at least one reason
        if not reasons:
            reasons.append("On the menu today")

        scored.append((score, RecommendedItem(
            name=item.name,
            category=item.category,
            price=item.price,
            is_veg=item.is_veg,
            is_spicy=item.is_spicy,
            reasons=reasons,
        )))

    # Sort by score descending, then name for stable ordering
    scored.sort(key=lambda t: (-t[0], t[1].name))
    return [item for _, item in scored[:limit]]
