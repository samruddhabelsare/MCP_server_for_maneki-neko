"""Tests for session binding, authentication, customer context, and phone normalization."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from maneki.mcp_customer import get_customer_context, get_menu_item, search_menu
from maneki.models import SessionContext
from maneki.services.auth import current_session_ctx
from maneki.services.customers import normalize_phone
from tests.conftest import FakeSupabase

REST_ID = uuid4()
CUST_ID = uuid4()
VALID_SESSION_ID = uuid4()
EXPIRED_SESSION_ID = uuid4()


@pytest.fixture(autouse=True)
def seed_data(fake_db: FakeSupabase) -> None:
    now = datetime.now(UTC)
    future = now + timedelta(hours=6)
    past = now - timedelta(hours=1)

    fake_db.seed(
        "sessions",
        [
            {
                "id": str(VALID_SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 5,
                "customer_id": str(CUST_ID),
                "character": "neko",
                "expires_at": future.isoformat(),
                "created_at": now.isoformat(),
            },
            {
                "id": str(EXPIRED_SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 2,
                "customer_id": str(CUST_ID),
                "character": "neko",
                "expires_at": past.isoformat(),
                "created_at": past.isoformat(),
            },
        ],
    )

    fake_db.seed(
        "customers",
        [
            {
                "id": str(CUST_ID),
                "restaurant_id": str(REST_ID),
                "name": "Alex",
                "phone": "+91-98765-43210",
                "visit_count": 3,
                "preferences": ["spicy", "extra wasabi"],
                "created_at": now.isoformat(),
            }
        ],
    )

    fake_db.seed(
        "menu_items",
        [
            {
                "id": str(uuid4()),
                "restaurant_id": str(REST_ID),
                "name": "Chicken Ramen",
                "category": "Main",
                "price": 14.50,
                "is_veg": False,
                "is_spicy": False,
                "is_available": True,
                "allergens": ["gluten", "soy"],
                "created_at": now.isoformat(),
            }
        ],
    )

    fake_db.seed(
        "orders",
        [
            {
                "id": str(uuid4()),
                "restaurant_id": str(REST_ID),
                "customer_id": str(CUST_ID),
                "table_number": 5,
                "items": [
                    {"name": "Chicken Ramen", "qty": 2, "price": 14.50},
                    {"name": "Matcha Ice Cream", "qty": 1, "price": 6.00},
                ],
                "total_amount": 35.00,
                "status": "delivered",
            }
        ],
    )


def test_phone_normalization() -> None:
    assert normalize_phone("9876543210") == "+91-98765-43210"
    assert normalize_phone("+91 98765-43210") == "+91-98765-43210"
    assert normalize_phone("919876543210") == "+91-98765-43210"
    assert normalize_phone("12345") == "12345"
    assert normalize_phone(None) is None


@pytest.mark.asyncio
async def test_tool_fails_without_session() -> None:
    res = await get_customer_context()
    assert res["ok"] is False
    assert res["error"] in ("unauthorized", "session_not_found")


@pytest.mark.asyncio
async def test_tool_fails_with_expired_session() -> None:
    token = current_session_ctx.set(
        SessionContext(
            session_id=EXPIRED_SESSION_ID,
            restaurant_id=REST_ID,
            table_number=2,
            customer_id=CUST_ID,
            character="neko",
        )
    )
    try:
        res = await get_customer_context()
        assert res["ok"] is False
        assert res["error"] == "session_expired"
    finally:
        current_session_ctx.reset(token)


@pytest.mark.asyncio
async def test_customer_context_with_valid_session() -> None:
    token = current_session_ctx.set(
        SessionContext(
            session_id=VALID_SESSION_ID,
            restaurant_id=REST_ID,
            table_number=5,
            customer_id=CUST_ID,
            character="neko",
        )
    )
    try:
        res = await get_customer_context()
        assert res["ok"] is True
        assert res["name"] == "Alex"
        assert res["visit_count"] == 3
        assert "Chicken Ramen" in res["top_favorites"]
        assert "spicy" in res["preferences"]
    finally:
        current_session_ctx.reset(token)


@pytest.mark.asyncio
async def test_guest_customer_context(fake_db: FakeSupabase) -> None:
    guest_sid = uuid4()
    now = datetime.now(UTC)
    fake_db.seed(
        "sessions",
        [
            {
                "id": str(guest_sid),
                "restaurant_id": str(REST_ID),
                "table_number": 3,
                "customer_id": None,
                "character": "neko",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            }
        ],
    )

    token = current_session_ctx.set(
        SessionContext(
            session_id=guest_sid,
            restaurant_id=REST_ID,
            table_number=3,
            customer_id=None,
            character="neko",
        )
    )
    try:
        res = await get_customer_context()
        assert res["ok"] is True
        assert res["guest"] is True
        assert res["name"] == "Guest"
        assert res["top_favorites"] == []
    finally:
        current_session_ctx.reset(token)


@pytest.mark.asyncio
async def test_search_and_get_menu_with_session() -> None:
    token = current_session_ctx.set(
        SessionContext(
            session_id=VALID_SESSION_ID,
            restaurant_id=REST_ID,
            table_number=5,
            customer_id=CUST_ID,
            character="neko",
        )
    )
    try:
        search_res = await search_menu(query="ramen")
        assert search_res["ok"] is True
        assert search_res["count"] == 1
        assert search_res["items"][0]["name"] == "Chicken Ramen"

        get_res = await get_menu_item(name="Chicken Ramen")
        assert get_res["ok"] is True
        assert get_res["item"]["name"] == "Chicken Ramen"
    finally:
        current_session_ctx.reset(token)
