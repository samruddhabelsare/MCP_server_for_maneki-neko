"""Sales and feedback analytics service.

Provides aggregated summaries for the admin MCP and REST.
All queries are scoped to a restaurant_id.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from maneki.config import get_settings
from maneki.db import get_db


def sales_summary(
    restaurant_id: str,
    days: int = 7,
    range: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Compute sales summary for a given time range or window.

    Supports:
        range: 'all' | '1d' | '7d' | '1m' | 'custom'
        start_date: YYYY-MM-DD (when range == 'custom')
        end_date: YYYY-MM-DD (when range == 'custom')
        days: Fallback day count (default 7, clamped 1-365)

    Returns:
        total_revenue:       Sum of total_amount for billed orders.
        total_orders:        Number of orders in the time frame.
        average_order_value: total_revenue / billed_orders count.
        top_items:           List of items with name, sold, and revenue.
        timeline:            Daily revenue and order count.
        orders_by_status:    Count by status.
        days:                Number of days if applicable.
    """
    cfg = get_settings()
    db = get_db()

    range_str = (range or "").lower().strip()
    if range_str == "1d":
        days = 1
    elif range_str == "7d":
        days = 7
    elif range_str == "1m":
        days = 30
    elif range_str not in ("all", "custom"):
        days = max(1, min(days, 365))

    res = (
        db.table(cfg.orders_table)
        .select("*")
        .eq("restaurant_id", str(restaurant_id))
        .execute()
    )
    all_orders = cast(list[dict[str, Any]], res.data) if res.data else []

    if range_str == "all":
        recent = all_orders
    elif range_str == "custom":
        recent = []
        for o in all_orders:
            created = str(o.get("created_at") or "")
            if not created:
                continue
            date_part = created[:10]
            if start_date and date_part < start_date:
                continue
            if end_date and date_part > end_date:
                continue
            recent.append(o)
    else:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        recent = [
            o for o in all_orders
            if o.get("created_at", "") >= cutoff
        ]

    item_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"sold": 0, "revenue": 0.0})
    total_revenue = 0.0
    status_counts: Counter[str] = Counter()
    billed_count = 0

    for o in recent:
        status = str(o.get("status", "unknown")).lower()
        status_counts[status] += 1

        if status != "cancelled":
            raw_items = o.get("items") or []
            if isinstance(raw_items, list):
                for it in raw_items:
                    if isinstance(it, dict) and "name" in it:
                        name = str(it["name"])
                        qty = int(it.get("qty", 1))
                        price = float(it.get("price", 0.0))
                        item_stats[name]["sold"] += qty
                        item_stats[name]["revenue"] = round(
                            item_stats[name]["revenue"] + (qty * price), 2
                        )

        if status == "billed":
            billed_count += 1
            total_revenue += float(o.get("total_amount") or 0.0)

    total_revenue = round(total_revenue, 2)
    average_order_value = (
        round(total_revenue / billed_count, 2) if billed_count > 0 else 0.0
    )

    top_items = [
        {
            "name": name,
            "sold": data["sold"],
            "quantity": data["sold"],  # backward compatibility for existing tests
            "revenue": round(data["revenue"], 2),
        }
        for name, data in sorted(item_stats.items(), key=lambda x: x[1]["sold"], reverse=True)[:10]
    ]

    # Timeline calculation: group by YYYY-MM-DD
    date_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    for o in recent:
        dt = str(o.get("created_at") or "")[:10]
        if dt:
            date_stats[dt]["orders"] += 1
            if str(o.get("status", "")).lower() == "billed":
                date_stats[dt]["revenue"] = round(
                    date_stats[dt]["revenue"] + float(o.get("total_amount") or 0.0), 2
                )

    timeline = [
        {"date": d, "revenue": s["revenue"], "orders": s["orders"]}
        for d, s in sorted(date_stats.items())
    ]

    return {
        "days": days,
        "total_orders": len(recent),
        "total_revenue": total_revenue,
        "average_order_value": average_order_value,
        "top_items": top_items,
        "timeline": timeline,
        "orders_by_status": dict(status_counts),
    }


def feedback_summary(
    restaurant_id: str,
    days: int = 7,
    range: str | None = None,
) -> dict[str, Any]:
    """Compute feedback summary for a given time range or window.

    Supports:
        range: 'all' | '7d' | '1m'
        days: Fallback day count (default 7, clamped 1-365)

    Returns:
        total_feedback:   Number of feedback records.
        total_reviews:    Alias for total_feedback.
        average_rating:   Float rating average.
        rating_breakdown: Dict {1: count, ..., 5: count}.
        distribution:     Dict {5: count, ..., 1: count}.
        recent_comments:  List of last 10 comment strings.
        detailed_comments: List of comment dicts with order_id, rating, comment, created_at.
        days:             The window requested.
    """
    cfg = get_settings()
    db = get_db()

    range_str = (range or "").lower().strip()
    if range_str == "1m":
        days = 30
    elif range_str == "7d":
        days = 7
    elif range_str == "all":
        days = 3650
    else:
        days = max(1, min(days, 365))

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

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

    if range_str == "all":
        recent_fb = [f for f in all_fb if str(f.get("order_id")) in order_ids]
    else:
        recent_fb = [
            f for f in all_fb
            if str(f.get("order_id")) in order_ids
            and f.get("created_at", "") >= cutoff
        ]

    import builtins

    ratings = [int(f["rating"]) for f in recent_fb if f.get("rating")]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    breakdown = {str(i): ratings.count(i) for i in builtins.range(1, 6)}
    distribution = {str(i): ratings.count(i) for i in builtins.range(5, 0, -1)}

    sorted_recent = sorted(recent_fb, key=lambda x: x.get("created_at", ""), reverse=True)
    comments = [
        str(f["comment"])
        for f in sorted_recent
        if f.get("comment")
    ][:10]

    detailed_comments = [
        {
            "order_id": str(f.get("order_id")),
            "rating": int(f.get("rating", 5)),
            "comment": str(f.get("comment")),
            "created_at": str(f.get("created_at") or ""),
        }
        for f in sorted_recent
        if f.get("comment")
    ][:10]

    return {
        "days": days,
        "total_feedback": len(recent_fb),
        "total_reviews": len(recent_fb),
        "average_rating": avg,
        "rating_breakdown": breakdown,
        "distribution": distribution,
        "recent_comments": comments,
        "detailed_comments": detailed_comments,
    }
