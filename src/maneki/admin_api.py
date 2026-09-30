"""FastAPI REST routes for Kitchen Display System (KDS) and Admin Dashboard.

Exposes standard HTTP endpoints backed by existing internal MCP services:
  - GET   /admin/orders/active          — Fetch active orders for KDS
  - PATCH /orders/{order_id}/status     — Advance order status (preparing, ready, etc.)
  - GET   /admin/analytics/sales        — Sales, revenue, timeline & top items
  - GET   /admin/analytics/feedback     — Ratings, distribution & recent reviews
  - GET   /admin/menu                   — Full menu catalog (including unavailable)
  - PATCH /admin/menu/{item_id}/availability — Mark item in stock / sold out
  - PATCH /admin/menu/{item_id}/price   — Update item price

All routes require the header:
  X-Admin-Key: <ADMIN_API_KEY>
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from maneki.config import get_settings
from maneki.errors import UnauthorizedError
from maneki.services.analytics import feedback_summary as svc_feedback_summary
from maneki.services.analytics import sales_summary as svc_sales_summary
from maneki.services.auth import current_admin_auth
from maneki.services.menu import (
    load_menu,
    set_item_availability_by_id,
    update_item_price_by_id,
)
from maneki.services.orders import list_active_orders, update_order_status

router = APIRouter(tags=["Admin & KDS"])


def verify_admin(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    """Validate that the incoming request contains a valid admin API key."""
    if current_admin_auth.get():
        return

    cfg = get_settings()
    if not x_admin_key or not secrets.compare_digest(x_admin_key, cfg.admin_api_key):
        raise UnauthorizedError("Invalid or missing admin key")


# ── Request Models ────────────────────────────────────────────────────────────

class UpdateOrderStatusRequest(BaseModel):
    status: str = Field(..., description="Target status: preparing, ready, delivered, cancelled")


class UpdateAvailabilityRequest(BaseModel):
    is_available: bool = Field(..., description="True if available, False if sold out")


class UpdatePriceRequest(BaseModel):
    price: float = Field(..., gt=0, description="New price (must be greater than 0)")


# ── KDS Endpoints ─────────────────────────────────────────────────────────────

@router.get("/admin/orders/active", dependencies=[Depends(verify_admin)])
async def get_active_orders(
    restaurant_id: Annotated[UUID, Query(description="UUID of the restaurant")],
) -> dict[str, Any]:
    """Fetch active orders (pending, preparing, ready) for KDS display.

    Items include resolved `is_veg` boolean flags and are sorted chronologically.
    """
    orders = list_active_orders(restaurant_id)
    return {"orders": orders}


@router.patch("/orders/{order_id}/status", dependencies=[Depends(verify_admin)])
async def patch_order_status(
    order_id: UUID,
    payload: UpdateOrderStatusRequest,
) -> dict[str, Any]:
    """Advance or update the lifecycle status of an order."""
    updated = update_order_status(order_id, payload.status)
    return {
        "success": True,
        "order_id": str(updated.id),
        "status": updated.status,
        "updated_at": datetime.now(UTC).isoformat(),
        "order": updated.model_dump(),
    }


# ── Admin Dashboard Endpoints ─────────────────────────────────────────────────

@router.get("/admin/analytics/sales", dependencies=[Depends(verify_admin)])
async def get_sales_analytics(
    restaurant_id: Annotated[UUID, Query(description="UUID of the restaurant")],
    range: Annotated[str, Query(description="Time range: all | 1d | 7d | 1m | custom")] = "7d",
    start_date: Annotated[str | None, Query(description="Start date (YYYY-MM-DD) for custom range")] = None,
    end_date: Annotated[str | None, Query(description="End date (YYYY-MM-DD) for custom range")] = None,
) -> dict[str, Any]:
    """Retrieve sales and revenue analytics summary for the admin dashboard."""
    data = svc_sales_summary(
        restaurant_id=str(restaurant_id),
        range=range,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "total_revenue": data["total_revenue"],
        "total_orders": data["total_orders"],
        "average_order_value": data["average_order_value"],
        "top_items": [
            {
                "name": item["name"],
                "sold": item["sold"],
                "revenue": item["revenue"],
            }
            for item in data.get("top_items", [])
        ],
        "timeline": data.get("timeline", []),
    }


@router.get("/admin/analytics/feedback", dependencies=[Depends(verify_admin)])
async def get_feedback_analytics(
    restaurant_id: Annotated[UUID, Query(description="UUID of the restaurant")],
    range: Annotated[str, Query(description="Time range: all | 7d | 1m")] = "7d",
) -> dict[str, Any]:
    """Retrieve feedback and rating analytics for the admin dashboard."""
    data = svc_feedback_summary(
        restaurant_id=str(restaurant_id),
        range=range,
    )
    return {
        "average_rating": data.get("average_rating"),
        "total_reviews": data.get("total_reviews", 0),
        "distribution": data.get("distribution", {}),
        "recent_comments": data.get("detailed_comments", []),
    }


@router.get("/admin/menu", dependencies=[Depends(verify_admin)])
async def get_admin_menu(
    restaurant_id: Annotated[UUID, Query(description="UUID of the restaurant")],
) -> dict[str, Any]:
    """Retrieve complete menu catalog including sold out / inactive items."""
    items = load_menu(restaurant_id, include_unavailable=True)
    return {
        "items": [
            {
                "id": str(item.id),
                "name": item.name,
                "category": item.category,
                "price": item.price,
                "is_veg": item.is_veg,
                "is_spicy": item.is_spicy,
                "is_available": item.is_available,
                "allergens": item.allergens,
            }
            for item in items
        ]
    }


@router.patch("/admin/menu/{item_id}/availability", dependencies=[Depends(verify_admin)])
async def patch_item_availability(
    item_id: UUID,
    payload: UpdateAvailabilityRequest,
) -> dict[str, Any]:
    """Toggle item stock status (available / sold out)."""
    set_item_availability_by_id(item_id, payload.is_available)
    return {
        "success": True,
        "item_id": str(item_id),
        "is_available": payload.is_available,
    }


@router.patch("/admin/menu/{item_id}/price", dependencies=[Depends(verify_admin)])
async def patch_item_price(
    item_id: UUID,
    payload: UpdatePriceRequest,
) -> dict[str, Any]:
    """Update item price."""
    update_item_price_by_id(item_id, payload.price)
    return {
        "success": True,
        "item_id": str(item_id),
        "price": payload.price,
    }
