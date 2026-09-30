"""Admin MCP server — kitchen/owner tools.

Mounted at /mcp/admin in main.py.
Every request must carry:
  - Header: X-Admin-Key: <ADMIN_API_KEY>

Admin tools:
  - list_orders
  - get_order
  - update_order_status
  - mark_billed
  - sales_summary
  - feedback_summary
  - set_item_availability
  - update_item_price
  - get_menu

Admin tools must NEVER be reachable through /mcp/customer and vice versa.
"""
from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from mcp.server.fastmcp import Context, FastMCP

from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import ManekiError
from maneki.models import err, ok
from maneki.services.analytics import (
    feedback_summary as svc_feedback_summary,
)
from maneki.services.analytics import (
    sales_summary as svc_sales_summary,
)
from maneki.services.auth import require_admin
from maneki.services.menu import (
    load_menu,
)
from maneki.services.menu import (
    set_item_availability as svc_set_availability,
)
from maneki.services.menu import (
    update_item_price as svc_update_price,
)
from maneki.services.orders import (
    get_order as svc_get_order,
)
from maneki.services.orders import (
    mark_billed as svc_mark_billed,
)
from maneki.services.orders import (
    update_order_status as svc_update_status,
)

mcp_admin = FastMCP(
    name="maneki-admin",
    instructions=(
        "Admin tools for kitchen staff and restaurant owners. "
        "Manage orders, update item availability, view sales summaries. "
        "Always pass restaurant_id explicitly. Never invent data."
    ),
)


# ── Order management ───────────────────────────────────────────────────────────

@mcp_admin.tool()
async def list_orders(
    restaurant_id: str,
    status: str | None = None,
    table_number: int | None = None,
    active_only: bool = False,
    limit: int = 20,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List orders for a restaurant with optional filters.

    Args:
        restaurant_id: UUID of the restaurant.
        status:        Filter by exact status (pending/preparing/ready/delivered/billed/cancelled).
        table_number:  Filter by table number.
        active_only:   If True, only returns orders not billed or cancelled.
        limit:         Max rows (1–100, default 20).
        admin_key:     Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        cfg = get_settings()
        db = get_db()
        limit = max(1, min(limit, 100))

        query = (
            db.table(cfg.orders_table)
            .select("*")
            .eq("restaurant_id", str(restaurant_id))
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status:
            query = query.eq("status", status.lower())
        if table_number is not None:
            query = query.eq("table_number", table_number)

        res = query.execute()
        rows = cast(list[dict[str, Any]], res.data) if res.data else []

        if active_only:
            rows = [r for r in rows if r.get("status") not in ("billed", "cancelled")]

        return ok(orders=rows, count=len(rows))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def get_order(
    order_id: str,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve a single order by ID.

    Args:
        order_id:  UUID of the order.
        admin_key: Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        order = svc_get_order(UUID(order_id))
        return ok(order=order.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def update_order_status(
    order_id: str,
    status: str,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Advance an order through the status lifecycle.

    Valid transitions: pending → preparing → ready → delivered → billed.
    cancelled is allowed from pending, preparing, ready.
    Invalid transitions are rejected with the allowed next states listed.

    Args:
        order_id:  UUID of the order.
        status:    Target status string.
        admin_key: Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        order = svc_update_status(UUID(order_id), status)
        return ok(order=order.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def mark_billed(
    order_id: str,
    payment_method: str = "cash",
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Record payment method and mark an order as billed.

    Args:
        order_id:       UUID of the order.
        payment_method: 'cash', 'card', 'upi', etc.
        admin_key:      Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        order = svc_mark_billed(UUID(order_id), payment_method=payment_method)
        return ok(order=order.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


# ── Analytics ──────────────────────────────────────────────────────────────────

@mcp_admin.tool()
async def sales_summary(
    restaurant_id: str,
    days: int = 7,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Get aggregated sales data for the last N days.

    Returns total orders, revenue (billed only), top 10 items by quantity,
    and order counts by status.

    Args:
        restaurant_id: UUID of the restaurant.
        days:          Lookback window (1–365, default 7).
        admin_key:     Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        summary = svc_sales_summary(str(restaurant_id), days=days)
        return ok(**summary)
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def feedback_summary(
    restaurant_id: str,
    days: int = 7,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Get feedback summary for the last N days.

    Returns average rating, rating breakdown (1–5), and recent comments.

    Args:
        restaurant_id: UUID of the restaurant.
        days:          Lookback window (1–365, default 7).
        admin_key:     Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        summary = svc_feedback_summary(str(restaurant_id), days=days)
        return ok(**summary)
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


# ── Menu management ────────────────────────────────────────────────────────────

@mcp_admin.tool()
async def set_item_availability(
    restaurant_id: str,
    name: str,
    is_available: bool,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Mark a menu item as available or unavailable (sold out / back in stock).

    Args:
        restaurant_id: UUID of the restaurant.
        name:          Item name (fuzzy matched: exact → prefix → contains).
        is_available:  True to mark in-stock, False to mark sold out.
        admin_key:     Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        item = svc_set_availability(
            restaurant_id=UUID(restaurant_id),
            name=name,
            is_available=is_available,
        )
        return ok(item=item.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def update_item_price(
    restaurant_id: str,
    name: str,
    price: float,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Update the price of a menu item.

    Note: Prices are re-read from DB at order-confirm time, so this takes
    effect immediately for future order confirmations.

    Args:
        restaurant_id: UUID of the restaurant.
        name:          Item name (fuzzy matched).
        price:         New price (must be > 0).
        admin_key:     Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        if price <= 0:
            return err("validation_error", "Price must be greater than 0.")
        item = svc_update_price(
            restaurant_id=UUID(restaurant_id),
            name=name,
            price=price,
        )
        return ok(item=item.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_admin.tool()
async def get_menu(
    restaurant_id: str,
    include_unavailable: bool = False,
    admin_key: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve the full menu for a restaurant.

    Args:
        restaurant_id:       UUID of the restaurant.
        include_unavailable: If True, also returns sold-out items.
        admin_key:           Admin API key (or send via X-Admin-Key header).
    """
    try:
        require_admin(ctx, admin_key)
        items = load_menu(
            restaurant_id=UUID(restaurant_id),
            include_unavailable=include_unavailable,
        )
        return ok(
            items=[item.model_dump() for item in items],
            count=len(items),
        )
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))
