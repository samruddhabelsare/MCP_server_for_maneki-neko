"""Pydantic v2 domain models.

These are shared across MCP tools, REST routes, and services.
All models are immutable (frozen) where appropriate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ── Menu ─────────────────────────────────────────────────────────────────────

class MenuItem(BaseModel):
    id: UUID
    restaurant_id: UUID
    name: str
    category: str
    price: float
    is_veg: bool = False
    is_spicy: bool = False
    is_available: bool = True
    allergens: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


# ── Customer ─────────────────────────────────────────────────────────────────

class Customer(BaseModel):
    id: UUID
    restaurant_id: UUID
    name: str
    phone: str | None = None
    visit_count: int = 1
    preferences: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


# ── Session ───────────────────────────────────────────────────────────────────

class Session(BaseModel):
    id: UUID
    restaurant_id: UUID
    table_number: int
    customer_id: UUID | None = None
    character: str = "neko"
    expires_at: datetime
    created_at: datetime | None = None


class SessionContext(BaseModel):
    """Resolved session data attached to every MCP tool call."""

    session_id: UUID
    restaurant_id: UUID
    table_number: int
    customer_id: UUID | None
    character: str


# ── Draft ─────────────────────────────────────────────────────────────────────

class DraftItem(BaseModel):
    name: str
    qty: int = Field(ge=1)
    price: float
    instructions: str = ""

    @field_validator("instructions")
    @classmethod
    def _cap_instructions(cls, v: str) -> str:
        return v[:200]


class Draft(BaseModel):
    id: UUID
    session_id: UUID
    restaurant_id: UUID
    items: list[DraftItem] = Field(default_factory=list)
    status: str = "open"
    order_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def total(self) -> float:
        return round(sum(i.price * i.qty for i in self.items), 2)


# ── Order ─────────────────────────────────────────────────────────────────────

# Valid status transitions per PRD Section 8 rule 8
ORDER_TRANSITIONS: dict[str, list[str]] = {
    "pending":   ["preparing", "cancelled"],
    "preparing": ["ready", "cancelled"],
    "ready":     ["delivered", "cancelled"],
    "delivered": ["billed"],
    "billed":    [],
    "cancelled": [],
}


class OrderItem(BaseModel):
    name: str
    qty: int
    price: float
    instructions: str = ""


class Order(BaseModel):
    id: UUID
    restaurant_id: UUID
    customer_id: UUID | None = None
    table_number: int
    items: list[OrderItem]
    total_amount: float
    status: str = "pending"
    payment_method: str = "cash"
    customer_phone: str | None = None
    bot_id: UUID | None = None
    created_at: datetime | None = None


# ── Feedback ──────────────────────────────────────────────────────────────────

class Feedback(BaseModel):
    id: UUID
    order_id: UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = None
    created_at: datetime | None = None


# ── Conversations ─────────────────────────────────────────────────────────────

class Message(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str  # 'user' | 'assistant' | 'tool'
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    created_at: datetime | None = None


class Conversation(BaseModel):
    id: UUID
    session_id: UUID
    restaurant_id: UUID
    created_at: datetime | None = None


# ── Recommendations ───────────────────────────────────────────────────────────

class RecommendedItem(BaseModel):
    name: str
    category: str
    price: float
    is_veg: bool
    is_spicy: bool
    reasons: list[str]  # Non-empty list of why this item is recommended


# ── API response shapes ───────────────────────────────────────────────────────

class OkResponse(BaseModel):
    ok: bool = True

    model_config = {"extra": "allow"}


def ok(**kwargs: Any) -> dict[str, Any]:
    """Convenience: build a successful MCP tool response dict."""
    return {"ok": True, **kwargs}


def err(code: str, message: str, **kwargs: Any) -> dict[str, Any]:
    """Convenience: build an error MCP tool response dict."""
    return {"ok": False, "error": code, "message": message, **kwargs}
