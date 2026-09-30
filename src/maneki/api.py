"""FastAPI REST routes — non-LLM endpoints.

Handles:
  - Session lifecycle: POST /sessions, GET /sessions/{id}/history
  - Order panel interactions: GET /sessions/{id}/draft, PATCH/DELETE draft items
  - Order placement: POST /sessions/{id}/confirm
  - Order polling & payment: GET /orders/{id}/status, POST /orders/{id}/mark-billed
  - Feedback: POST /orders/{id}/feedback
  - Health check: GET /healthz
"""
from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from maneki.db import get_db
from maneki.errors import UnauthorizedError, ValidationError
from maneki.models import ok
from maneki.services.conversations import load_messages
from maneki.services.customers import upsert_customer
from maneki.services.drafts import (
    format_draft,
    get_current_draft,
    remove_item,
    set_quantity,
)
from maneki.services.feedback import submit_feedback
from maneki.services.orders import (
    confirm_order,
    get_order_status,
    list_customer_history,
    mark_billed,
)
from maneki.services.sessions import create_session, get_session

router = APIRouter()

_start_time = time.time()


# ── Request Models ────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    restaurant_id: UUID
    table_number: int = Field(ge=1)
    phone: str | None = None
    name: str | None = None
    preferences: list[str] | None = None
    character: str = "neko"
    guest: bool = False


class PatchDraftItemRequest(BaseModel):
    qty: int = Field(ge=0)


class MarkBilledRequest(BaseModel):
    payment_method: str = "cash"


class SubmitFeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = None


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/healthz", tags=["ops"])
async def healthz() -> JSONResponse:
    """Liveness probe — returns 200 if the service is up.

    Also checks Supabase connectivity so Render health checks
    catch DB misconfiguration early.
    """
    db_ok = False
    db_error: str | None = None

    try:
        db = get_db()
        db.table("restaurants").select("id").limit(1).execute()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "supabase": "ok" if db_ok else f"error: {db_error}",
        "version": "0.1.0",
    }
    return JSONResponse(
        content=payload,
        status_code=200 if db_ok else 503,
    )


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.post("/sessions", tags=["sessions"])
async def create_session_endpoint(payload: CreateSessionRequest) -> dict[str, Any]:
    """Start a dining session for a table.

    Creates/updates customer profile if phone is provided.
    Name is required for newly registered customers.
    """
    customer_id: UUID | None = None
    customer_data: dict[str, Any] | None = None

    if not payload.guest and payload.phone:
        customer = upsert_customer(
            restaurant_id=payload.restaurant_id,
            phone=payload.phone,
            name=payload.name,
            preferences=payload.preferences,
        )
        customer_id = customer.id
        customer_data = customer.model_dump()
    elif not payload.guest and not payload.phone and payload.name:
        customer = upsert_customer(
            restaurant_id=payload.restaurant_id,
            phone=None,
            name=payload.name,
            preferences=payload.preferences,
        )
        customer_id = customer.id
        customer_data = customer.model_dump()

    session = create_session(
        restaurant_id=payload.restaurant_id,
        table_number=payload.table_number,
        customer_id=customer_id,
        character=payload.character,
    )

    return ok(
        session_id=str(session.id),
        restaurant_id=str(session.restaurant_id),
        table_number=session.table_number,
        customer=customer_data,
        character=session.character,
        expires_at=session.expires_at.isoformat(),
    )


@router.get("/sessions/{id}/draft", tags=["drafts"])
async def get_session_draft_endpoint(
    id: UUID,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Retrieve the current order draft and total for the session's order panel."""
    session = _verify_session_access(id, authorization)
    draft = get_current_draft(session.id, session.restaurant_id)
    return ok(draft=format_draft(draft))


@router.patch("/sessions/{id}/draft/items/{name}", tags=["drafts"])
async def patch_draft_item_endpoint(
    id: UUID,
    name: str,
    payload: PatchDraftItemRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Update item quantity from order panel UI (qty=0 removes)."""
    session = _verify_session_access(id, authorization)
    draft = set_quantity(
        session_id=session.id,
        restaurant_id=session.restaurant_id,
        name=name,
        qty=payload.qty,
    )
    return ok(draft=format_draft(draft))


@router.delete("/sessions/{id}/draft/items/{name}", tags=["drafts"])
async def delete_draft_item_endpoint(
    id: UUID,
    name: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Delete an item line from the draft via UI panel ✕ button."""
    session = _verify_session_access(id, authorization)
    draft = remove_item(
        session_id=session.id,
        restaurant_id=session.restaurant_id,
        name=name,
    )
    return ok(draft=format_draft(draft))


@router.post("/sessions/{id}/confirm", tags=["orders"])
async def confirm_session_order_endpoint(
    id: UUID,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """The only endpoint that places an order.

    Atomically converts open draft into an order via Postgres RPC.
    Recomputes total from live database menu prices.
    Idempotent: repeating call returns the placed order without duplicating.
    """
    _verify_session_access(id, authorization)
    order = confirm_order(session_id=id)
    return ok(order=order.model_dump())


@router.get("/sessions/{id}/history", tags=["customers"])
async def get_session_history_endpoint(
    id: UUID,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Retrieve customer's past orders (last 10)."""
    session = _verify_session_access(id, authorization)
    if not session.customer_id:
        return ok(orders=[])

    orders = list_customer_history(
        customer_id=session.customer_id,
        restaurant_id=session.restaurant_id,
        limit=10,
    )
    return ok(orders=[o.model_dump() for o in orders])


@router.get("/sessions/{id}/messages", tags=["conversations"])
async def get_session_messages_endpoint(
    id: UUID,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Load conversation messages for chat restoration on page refresh or device switch.

    Returns the last 50 messages in chronological order.
    """
    session = _verify_session_access(id, authorization)
    messages = load_messages(
        session_id=session.id,
        restaurant_id=session.restaurant_id,
        limit=50,
    )
    return ok(messages=[m.model_dump() for m in messages])


# ── Orders ────────────────────────────────────────────────────────────────────

@router.get("/orders/{id}/status", tags=["orders"])
async def get_order_status_endpoint(id: UUID) -> dict[str, Any]:
    """Get status of an order for frontend polling."""
    status_data = get_order_status(id)
    return ok(**status_data)


@router.post("/orders/{id}/mark-billed", tags=["orders"])
async def mark_billed_endpoint(id: UUID, payload: MarkBilledRequest) -> dict[str, Any]:
    """Record payment method and mark order as billed."""
    order = mark_billed(order_id=id, payment_method=payload.payment_method)
    return ok(order=order.model_dump())


@router.post("/orders/{id}/feedback", tags=["orders"])
async def submit_feedback_endpoint(id: UUID, payload: SubmitFeedbackRequest) -> dict[str, Any]:
    """Submit rating and comments for a placed order."""
    if payload.rating < 1 or payload.rating > 5:
        raise ValidationError("Rating must be between 1 and 5.")

    fb = submit_feedback(order_id=id, rating=payload.rating, comment=payload.comment)
    return ok(feedback=fb.model_dump())


# ── Helper ────────────────────────────────────────────────────────────────────

def _verify_session_access(session_id: UUID, authorization: str | None) -> Any:
    """Validate that the session exists, is unexpired, and token matches if provided."""
    session = get_session(session_id)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        if token != str(session_id):
            raise UnauthorizedError("Session authorization header does not match requested session ID")
    return session
