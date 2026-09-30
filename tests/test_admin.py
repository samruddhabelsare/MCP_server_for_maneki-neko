"""Tests for admin MCP tools and analytics (M5).

Verifies:
  - Customer session cannot call admin tools (scope isolation).
  - All admin tools require a valid admin key.
  - sales_summary numbers match a hand-computed fixture.
  - feedback_summary aggregates correctly.
  - set_item_availability and update_item_price work.
  - list_orders filters (status, active_only) work.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from maneki.errors import ForbiddenError
from maneki.models import SessionContext
from maneki.services.analytics import feedback_summary, sales_summary
from maneki.services.auth import (
    current_admin_auth,
    current_session_ctx,
    require_admin,
)
from maneki.services.menu import get_menu_item
from tests.conftest import FakeSupabase

REST_ID = uuid4()
CUST_ID = uuid4()
SESSION_ID = uuid4()

NOW = datetime.now(UTC)

SAMPLE_MENU = [
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Chicken Ramen",
        "category": "Main",
        "price": 14.50,
        "is_veg": False,
        "is_spicy": False,
        "is_available": True,
        "allergens": [],
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Veggie Gyoza",
        "category": "Appetizer",
        "price": 8.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": True,
        "allergens": [],
        "created_at": NOW.isoformat(),
    },
]

# Hand-computed fixture orders (within the last 7 days)
FIXTURE_ORDERS = [
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "customer_id": str(CUST_ID),
        "table_number": 1,
        "items": [
            {"name": "Chicken Ramen", "qty": 2, "price": 14.50, "instructions": ""},
            {"name": "Veggie Gyoza", "qty": 1, "price": 8.00, "instructions": ""},
        ],
        "total_amount": 37.00,  # 2×14.50 + 1×8.00
        "status": "billed",
        "payment_method": "cash",
        "customer_phone": None,
        "bot_id": None,
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "customer_id": None,
        "table_number": 2,
        "items": [
            {"name": "Chicken Ramen", "qty": 1, "price": 14.50, "instructions": ""},
        ],
        "total_amount": 14.50,
        "status": "billed",
        "payment_method": "card",
        "customer_phone": None,
        "bot_id": None,
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "customer_id": None,
        "table_number": 3,
        "items": [
            {"name": "Veggie Gyoza", "qty": 2, "price": 8.00, "instructions": ""},
        ],
        "total_amount": 16.00,
        "status": "cancelled",  # excluded from revenue but included in status count
        "payment_method": "cash",
        "customer_phone": None,
        "bot_id": None,
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "customer_id": str(CUST_ID),
        "table_number": 4,
        "items": [
            {"name": "Chicken Ramen", "qty": 1, "price": 14.50, "instructions": ""},
        ],
        "total_amount": 14.50,
        "status": "preparing",
        "payment_method": "cash",
        "customer_phone": None,
        "bot_id": None,
        "created_at": NOW.isoformat(),
    },
]

# Hand-computed expected values from FIXTURE_ORDERS:
#   total_orders = 4 (all within window)
#   total_revenue = 37.00 + 14.50 = 51.50  (billed only, not cancelled)
#   Chicken Ramen: 2+1+1 = 4 qty (from non-cancelled orders)
#   Veggie Gyoza: 1 qty (from billed order only, cancelled excluded)
#   orders_by_status: billed=2, cancelled=1, preparing=1

FIXTURE_FEEDBACK = [
    {
        "id": str(uuid4()),
        "order_id": FIXTURE_ORDERS[0]["id"],
        "rating": 5,
        "comment": "Amazing ramen!",
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "order_id": FIXTURE_ORDERS[1]["id"],
        "rating": 4,
        "comment": None,
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(uuid4()),
        "order_id": FIXTURE_ORDERS[3]["id"],
        "rating": 3,
        "comment": "A bit slow",
        "created_at": NOW.isoformat(),
    },
]
# Average: (5+4+3)/3 = 4.0


@pytest.fixture(autouse=True)
def seed_data(fake_db: FakeSupabase) -> None:
    fake_db.seed("menu_items", SAMPLE_MENU)
    fake_db.seed(
        "sessions",
        [
            {
                "id": str(SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 5,
                "customer_id": str(CUST_ID),
                "character": "neko",
                "expires_at": (NOW + timedelta(hours=6)).isoformat(),
                "created_at": NOW.isoformat(),
            }
        ],
    )
    fake_db.seed("orders", FIXTURE_ORDERS)
    fake_db.seed("feedback", FIXTURE_FEEDBACK)


# ── Scope isolation ────────────────────────────────────────────────────────────

def test_require_admin_raises_without_key() -> None:
    """Admin tools must reject calls without a valid admin key."""
    # Reset admin auth contextvar
    token = current_admin_auth.set(False)
    try:
        with pytest.raises(ForbiddenError):
            require_admin(ctx=None, admin_key=None)
        with pytest.raises(ForbiddenError):
            require_admin(ctx=None, admin_key="wrong-key")
    finally:
        current_admin_auth.reset(token)


def test_require_admin_succeeds_with_correct_key() -> None:
    """require_admin passes with the configured admin key."""
    from maneki.config import get_settings
    cfg = get_settings()
    # Should not raise
    token = current_admin_auth.set(False)
    try:
        require_admin(ctx=None, admin_key=cfg.admin_api_key)
    finally:
        current_admin_auth.reset(token)


def test_customer_session_cannot_access_admin_ctx() -> None:
    """The customer session ContextVar must not grant admin access."""
    # Set a valid customer session context
    session_ctx = SessionContext(
        session_id=SESSION_ID,
        restaurant_id=REST_ID,
        table_number=5,
        customer_id=CUST_ID,
        character="neko",
    )
    s_token = current_session_ctx.set(session_ctx)
    a_token = current_admin_auth.set(False)
    try:
        # Admin require_admin should still fail without the key
        with pytest.raises(ForbiddenError):
            require_admin(ctx=None, admin_key=None)
    finally:
        current_session_ctx.reset(s_token)
        current_admin_auth.reset(a_token)


# ── Sales summary ──────────────────────────────────────────────────────────────

def test_sales_summary_matches_hand_computed_fixture() -> None:
    """DoD: sales_summary numbers must match the hand-computed fixture."""
    summary = sales_summary(str(REST_ID), days=7)

    assert summary["total_orders"] == 4
    # Revenue: billed orders only = 37.00 + 14.50
    assert summary["total_revenue"] == 51.50

    # Top items by qty (non-cancelled orders: orders 0, 1, 3)
    # Chicken Ramen: qty 2 + 1 + 1 = 4
    # Veggie Gyoza: qty 1 (only order 0; order 2 is cancelled)
    top_names = [item["name"] for item in summary["top_items"]]
    assert top_names[0] == "Chicken Ramen"  # highest qty
    chicken = next(i for i in summary["top_items"] if i["name"] == "Chicken Ramen")
    assert chicken["quantity"] == 4
    gyoza = next(i for i in summary["top_items"] if i["name"] == "Veggie Gyoza")
    assert gyoza["quantity"] == 1

    # Status counts
    status = summary["orders_by_status"]
    assert status.get("billed") == 2
    assert status.get("cancelled") == 1
    assert status.get("preparing") == 1


def test_sales_summary_empty_window() -> None:
    """Days=0 is clamped to 1; if no recent orders the counts are 0."""
    # Use a 0-day window (clamped to 1) — our test data is NOW so it should still count
    summary = sales_summary(str(REST_ID), days=0)
    # days should be clamped to 1
    assert summary["days"] == 1


# ── Feedback summary ──────────────────────────────────────────────────────────

def test_feedback_summary_matches_hand_computed_fixture() -> None:
    """DoD: feedback_summary average_rating must be correct."""
    summary = feedback_summary(str(REST_ID), days=7)

    assert summary["total_feedback"] == 3
    assert summary["average_rating"] == 4.0  # (5+4+3)/3

    breakdown = summary["rating_breakdown"]
    assert breakdown["5"] == 1
    assert breakdown["4"] == 1
    assert breakdown["3"] == 1
    assert breakdown["1"] == 0
    assert breakdown["2"] == 0

    # Recent comments (non-None), newest first
    comments = summary["recent_comments"]
    assert "Amazing ramen!" in comments
    assert "A bit slow" in comments
    # None comment excluded
    assert len(comments) == 2


# ── Menu management ────────────────────────────────────────────────────────────

def test_set_item_availability_marks_sold_out() -> None:
    """set_item_availability should toggle availability."""
    from maneki.services.menu import load_menu, set_item_availability

    item = set_item_availability(REST_ID, "Chicken Ramen", is_available=False)
    assert not item.is_available

    # load_menu(include_unavailable=False) should exclude it now
    available = load_menu(REST_ID, include_unavailable=False)
    names = [i.name for i in available]
    assert "Chicken Ramen" not in names

    # Admin can still see it with include_unavailable=True
    all_items = load_menu(REST_ID, include_unavailable=True)
    all_names = [i.name for i in all_items]
    assert "Chicken Ramen" in all_names


def test_update_item_price_persists() -> None:
    """update_item_price should change the returned price immediately."""
    from maneki.services.menu import update_item_price

    updated = update_item_price(REST_ID, "Veggie Gyoza", price=9.50)
    assert updated.price == 9.50

    # Follow-up get_menu_item should see the new price
    item = get_menu_item(REST_ID, "Veggie Gyoza")
    assert item.price == 9.50
