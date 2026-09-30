"""Tests for order confirmation, live re-pricing, lifecycle transitions, and history (M3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from maneki.errors import (
    EmptyDraftError,
    InvalidStatusTransitionError,
    ValidationError,
)
from maneki.services.drafts import add_item
from maneki.services.feedback import submit_feedback
from maneki.services.menu import update_item_price
from maneki.services.orders import (
    confirm_order,
    list_customer_history,
    mark_billed,
    update_order_status,
)
from tests.conftest import FakeSupabase

REST_ID = uuid4()
CUST_ID = uuid4()
SESSION_ID = uuid4()
GUEST_SESSION_ID = uuid4()

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
        "allergens": ["gluten"],
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Matcha Ice Cream",
        "category": "Dessert",
        "price": 5.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": True,
        "allergens": ["dairy"],
        "created_at": "2026-09-30T00:00:00Z",
    },
]


@pytest.fixture(autouse=True)
def setup_data(fake_db: FakeSupabase) -> None:
    now = datetime.now(UTC)
    fake_db.seed("menu_items", SAMPLE_MENU)
    fake_db.seed(
        "sessions",
        [
            {
                "id": str(SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 3,
                "customer_id": str(CUST_ID),
                "character": "neko",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            },
            {
                "id": str(GUEST_SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 7,
                "customer_id": None,
                "character": "neko",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            },
        ],
    )


def test_confirm_order_atomic_and_idempotent(fake_db: FakeSupabase) -> None:
    """DoD: Confirm twice -> exactly one order; second call returns the same order."""
    add_item(session_id=SESSION_ID, restaurant_id=REST_ID, name="Chicken Ramen", qty=2)

    order1 = confirm_order(SESSION_ID)
    assert order1.status == "pending"
    assert order1.total_amount == 29.00
    assert len(order1.items) == 1
    assert order1.items[0].qty == 2

    # Second call (idempotent retry)
    order2 = confirm_order(SESSION_ID)
    assert order2.id == order1.id

    # Verify no second row was inserted in DB
    all_orders = [o for o in fake_db._tables["orders"] if str(o.get("restaurant_id")) == str(REST_ID)]
    assert len(all_orders) == 1


def test_confirm_order_recomputes_prices_from_live_menu() -> None:
    """PRD Rule 3 & DoD: Prices recomputed from menu table at confirm time."""
    # Added at 14.50
    add_item(session_id=SESSION_ID, restaurant_id=REST_ID, name="Chicken Ramen", qty=2)

    # Price changes in the restaurant before confirm
    update_item_price(restaurant_id=REST_ID, name="Chicken Ramen", price=18.00)

    # Confirm order — total must reflect the new 18.00 price
    order = confirm_order(SESSION_ID)
    assert order.items[0].price == 18.00
    assert order.total_amount == 36.00


def test_confirm_empty_draft_raises() -> None:
    with pytest.raises(EmptyDraftError):
        confirm_order(SESSION_ID)


def test_status_lifecycle_and_invalid_transition() -> None:
    """PRD Rule 8 & DoD: pending -> preparing -> ready -> delivered -> billed. Invalid rejected."""
    add_item(session_id=SESSION_ID, restaurant_id=REST_ID, name="Chicken Ramen", qty=1)
    order = confirm_order(SESSION_ID)
    oid = order.id

    # Invalid: pending -> delivered (must go through preparing -> ready)
    with pytest.raises(InvalidStatusTransitionError) as exc_info:
        update_order_status(oid, "delivered")
    assert "pending" in str(exc_info.value)
    assert "delivered" in str(exc_info.value)

    # Valid lifecycle:
    o_prep = update_order_status(oid, "preparing")
    assert o_prep.status == "preparing"

    o_ready = update_order_status(oid, "ready")
    assert o_ready.status == "ready"

    o_deliv = update_order_status(oid, "delivered")
    assert o_deliv.status == "delivered"

    # Delivered cannot be cancelled
    with pytest.raises(InvalidStatusTransitionError):
        update_order_status(oid, "cancelled")

    # Mark billed
    o_billed = mark_billed(oid, payment_method="card")
    assert o_billed.status == "billed"
    assert o_billed.payment_method == "card"


def test_guest_flow_works() -> None:
    """DoD: Guest flow can add items, confirm order, and query status."""
    add_item(session_id=GUEST_SESSION_ID, restaurant_id=REST_ID, name="Matcha Ice Cream", qty=3)
    order = confirm_order(GUEST_SESSION_ID)

    assert order.customer_id is None
    assert order.table_number == 7
    assert order.total_amount == 15.00
    assert order.status == "pending"


def test_feedback_submission() -> None:
    add_item(session_id=SESSION_ID, restaurant_id=REST_ID, name="Matcha Ice Cream", qty=1)
    order = confirm_order(SESSION_ID)

    fb = submit_feedback(order_id=order.id, rating=5, comment="Delicious matcha!")
    assert fb.rating == 5
    assert fb.comment == "Delicious matcha!"

    # Out of range rating
    with pytest.raises(ValidationError):
        submit_feedback(order_id=order.id, rating=6)
    with pytest.raises(ValidationError):
        submit_feedback(order_id=order.id, rating=0)


def test_customer_order_history() -> None:
    add_item(session_id=SESSION_ID, restaurant_id=REST_ID, name="Chicken Ramen", qty=1)
    confirm_order(SESSION_ID)

    history = list_customer_history(customer_id=CUST_ID, restaurant_id=REST_ID)
    assert len(history) >= 1
    assert history[0].customer_id == CUST_ID
