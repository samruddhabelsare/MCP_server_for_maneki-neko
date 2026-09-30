"""Sales and feedback analytics service.

Provides aggregated summaries for the admin MCP and REST.
All queries are scoped to a restaurant_id.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, cast

from maneki.config import get_settings
from maneki.db import get_db


def sales_summary(restaurant_id: str, days: int = 7) -> dict[str, Any]:
    """Compute sales summary for the last `days` days.

    Returns:
        total_orders:     Number of orders (all statuses except cancelled).
        total_revenue:    Sum of total_amount for billed orders.
        top_items:        List of {name, quantity} sorted by quantity desc (top 10).
        orders_by_status: Dict mapping status -> count.
        days:             The window requested.
    """
    cfg = get_settings()
    db = get_db()
    days = max(1, min(days, 365))

    res = (
        db.table(cfg.orders_table)
        .select("*")
        .eq("restaurant_id", str(restaurant_id))
        .execute()
    )
    all_orders = cast(list[dict[str, Any]], res.data) if res.data else []

    # Filter by recency using created_at (string comparison works for ISO format)
    from datetime import UTC, datetime, timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    recent = [
        o for o in all_orders
        if o.get("created_at", "") >= cutoff
    ]

    item_counts: Counter[str] = Counter()
    total_revenue = 0.0
    status_counts: Counter[str] = Counter()

    for o in recent:
        status = str(o.get("status", "unknown"))
        status_counts[status] += 1

        if status not in ("cancelled",):
            raw_items = o.get("items") or []
            if isinstance(raw_items, list):
                for it in raw_items:
                    if isinstance(it, dict) and "name" in it:
                        item_counts[str(it["name"])] += int(it.get("qty", 1))

        if status == "billed":
            total_revenue += float(o.get("total_amount") or 0.0)

    top_items = [
        {"name": name, "quantity": qty}
        for name, qty in item_counts.most_common(10)
    ]

    return {
        "days": days,
        "total_orders": len(recent),
        "total_revenue": round(total_revenue, 2),
        "top_items": top_items,
        "orders_by_status": dict(status_counts),
    }


def feedback_summary(restaurant_id: str, days: int = 7) -> dict[str, Any]:
    """Compute feedback summary for the last `days` days.

    Returns:
        total_feedback:   Number of feedback records.
        average_rating:   Float, or null if no feedback.
        rating_breakdown: Dict {1: count, 2: count, ..., 5: count}.
        recent_comments:  List of last 10 non-empty comments (newest first).
        days:             The window requested.
    """
    cfg = get_settings()
    db = get_db()
    days = max(1, min(days, 365))

    from datetime import UTC, datetime, timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    # Get feedback joined with orders for restaurant filtering
    # We rely on order_id being in feedback; look up orders for the restaurant
    orders_res = (
        db.table(cfg.orders_table)
        .select("id")
        .eq("restaurant_id", str(restaurant_id))
        .execute()
    )
    order_ids = {
        str(o["id"])
        for o in (cast(list[dict[str, Any]], orders_res.data) if orders_res.data else [])
    }

    fb_res = (
        db.table(cfg.feedback_table)
        .select("*")
        .execute()
    )
    all_fb = cast(list[dict[str, Any]], fb_res.data) if fb_res.data else []

    recent_fb = [
        f for f in all_fb
        if str(f.get("order_id")) in order_ids
        and f.get("created_at", "") >= cutoff
    ]

    ratings = [int(f["rating"]) for f in recent_fb if f.get("rating")]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    breakdown = {str(i): ratings.count(i) for i in range(1, 6)}

    comments = [
        str(f["comment"])
        for f in sorted(recent_fb, key=lambda x: x.get("created_at", ""), reverse=True)
        if f.get("comment")
    ][:10]

    return {
        "days": days,
        "total_feedback": len(recent_fb),
        "average_rating": avg,
        "rating_breakdown": breakdown,
        "recent_comments": comments,
    }
