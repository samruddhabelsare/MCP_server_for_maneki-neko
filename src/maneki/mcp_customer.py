"""Customer MCP server — session-scoped tools.

Mounted at /mcp/customer in main.py.
Every request must carry:
  - Header: X-Session-Id: <uuid>
  - Header: Authorization: Bearer <INTERNAL_MCP_TOKEN>

Customer-scoped tools:
  - get_customer_context
  - search_menu
  - get_menu_item
  - get_current_order
  - add_item
  - remove_item
  - set_quantity
  - clear_order
  - get_order_status
  - recommend_dishes
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from maneki.errors import ManekiError
from maneki.models import err, ok
from maneki.services.auth import require_session
from maneki.services.customers import get_customer_context as svc_get_customer_context
from maneki.services.drafts import (
    add_item as svc_add_item,
)
from maneki.services.drafts import (
    clear_draft as svc_clear_draft,
)
from maneki.services.drafts import (
    format_draft,
    get_current_draft,
)
from maneki.services.drafts import (
    remove_item as svc_remove_item,
)
from maneki.services.drafts import (
    set_quantity as svc_set_quantity,
)
from maneki.services.menu import get_menu_item as svc_get_menu_item
from maneki.services.menu import search_menu as svc_search_menu
from maneki.services.orders import get_latest_active_order
from maneki.services.recommend import recommend_dishes as svc_recommend_dishes

mcp_customer = FastMCP(
    name="maneki-customer",
    instructions=(
        "You are a helpful AI waiter. Use these tools to look up the menu, "
        "manage the customer draft order, and answer questions about order status. "
        "Never invent items or prices. Never claim the order is placed — "
        "tell the customer to press the Confirm button."
    ),
)


@mcp_customer.tool()
async def get_customer_context(ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Retrieve customer profile and dining history for this session.

    Returns the customer's name, preferences, visit count, and top 5 favorite dishes.
    For guest sessions, returns minimal guest information.
    """
    try:
        session = require_session(ctx)
        data = svc_get_customer_context(session)
        return ok(**data)
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def search_menu(
    query: str | None = None,
    category: str | None = None,
    veg_only: bool = False,
    spicy: bool | None = None,
    max_price: float | None = None,
    limit: int = 10,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Search for available menu items using filters.

    Filters combine with AND logic.
    Returns up to `limit` available matching items with prices and descriptions.
    """
    try:
        session = require_session(ctx)
        q = query[:100] if query else None
        items = svc_search_menu(
            restaurant_id=session.restaurant_id,
            query=q,
            category=category,
            veg_only=veg_only,
            spicy=spicy,
            max_price=max_price,
            limit=min(limit, 50),
        )
        return ok(items=[item.model_dump() for item in items], count=len(items))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def get_menu_item(name: str, ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Look up a specific menu item by name.

    Uses exact -> prefix -> contains matching.
    If the name matches multiple candidates, returns an ambiguous result listing options.
    """
    try:
        session = require_session(ctx)
        clean_name = name[:100]
        item = svc_get_menu_item(restaurant_id=session.restaurant_id, name=clean_name)
        return ok(item=item.model_dump())
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def get_current_order(ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Get the current draft order for this table/session.

    Returns the list of items in the draft, per-line subtotals, and total price.
    """
    try:
        session = require_session(ctx)
        draft = get_current_draft(session.session_id, session.restaurant_id)
        return ok(draft=format_draft(draft))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def add_item(
    name: str,
    qty: int = 1,
    instructions: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Add a quantity of an item to the current order draft.

    Resolves the canonical menu item name and price automatically.
    Adds to the existing quantity if already in the draft.
    Returns the updated order draft.
    """
    try:
        session = require_session(ctx)
        clean_name = name[:100]
        clean_inst = instructions[:200]
        draft = svc_add_item(
            session_id=session.session_id,
            restaurant_id=session.restaurant_id,
            name=clean_name,
            qty=qty,
            instructions=clean_inst,
        )
        return ok(draft=format_draft(draft))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def remove_item(name: str, ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Remove a dish completely from the current order draft."""
    try:
        session = require_session(ctx)
        clean_name = name[:100]
        draft = svc_remove_item(
            session_id=session.session_id,
            restaurant_id=session.restaurant_id,
            name=clean_name,
        )
        return ok(draft=format_draft(draft))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def set_quantity(
    name: str,
    qty: int,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Set the exact quantity of an item in the current order draft.

    Setting quantity to 0 removes the item.
    """
    try:
        session = require_session(ctx)
        clean_name = name[:100]
        draft = svc_set_quantity(
            session_id=session.session_id,
            restaurant_id=session.restaurant_id,
            name=clean_name,
            qty=qty,
        )
        return ok(draft=format_draft(draft))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def clear_order(ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Clear all items from the current order draft."""
    try:
        session = require_session(ctx)
        draft = svc_clear_draft(
            session_id=session.session_id,
            restaurant_id=session.restaurant_id,
        )
        return ok(draft=format_draft(draft))
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def get_order_status(ctx: Context[Any, Any] | None = None) -> dict[str, Any]:
    """Get the status of the latest active order for this session.

    Returns the most recent non-billed, non-cancelled order for this table.
    Useful for customers asking 'where is my order?' or 'is it ready yet?'
    Returns null if no active order exists (i.e. nothing confirmed yet).
    """
    try:
        session = require_session(ctx)
        order = get_latest_active_order(session.session_id)
        if order is None:
            return ok(order=None, message="No active order found for this session.")
        return ok(
            order_id=str(order.id),
            status=order.status,
            table_number=order.table_number,
            items=[it.model_dump() for it in order.items],
            total_amount=order.total_amount,
            created_at=order.created_at.isoformat() if order.created_at else None,
        )
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))


@mcp_customer.tool()
async def recommend_dishes(
    veg_only: bool = False,
    exclude_spicy: bool = False,
    limit: int = 6,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Get personalized dish recommendations for this session.

    Ranks candidates by: past order history, customer preferences, and
    restaurant-wide popularity. Each result includes a `reasons` list
    explaining why it was suggested — use these to phrase your suggestion
    naturally. Do NOT invent additional reasons.

    Args:
        veg_only:      Only suggest vegetarian items.
        exclude_spicy: Exclude spicy items.
        limit:         Number of recommendations (1–20, default 6).
    """
    try:
        session = require_session(ctx)
        results = svc_recommend_dishes(
            restaurant_id=session.restaurant_id,
            customer_id=session.customer_id,
            veg_only=veg_only,
            exclude_spicy=exclude_spicy,
            limit=limit,
        )
        return ok(
            recommendations=[r.model_dump() for r in results],
            count=len(results),
        )
    except ManekiError as exc:
        return exc.to_dict()
    except Exception as exc:
        return err("internal_error", str(exc))
