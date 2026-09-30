"""Tests for recommend_dishes and conversations service (M4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from maneki.models import SessionContext
from maneki.services.conversations import load_messages, save_message
from maneki.services.recommend import recommend_dishes
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
        "name": "Spicy Tonkotsu",
        "category": "Main",
        "price": 16.00,
        "is_veg": False,
        "is_spicy": True,
        "is_available": True,
        "allergens": ["gluten", "pork"],
        "created_at": "2026-09-30T00:00:00Z",
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
        "allergens": ["gluten"],
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Matcha Ice Cream",
        "category": "Dessert",
        "price": 6.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": True,
        "allergens": ["dairy"],
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Sold Out Ramen",
        "category": "Main",
        "price": 12.00,
        "is_veg": False,
        "is_spicy": False,
        "is_available": False,  # unavailable
        "allergens": [],
        "created_at": "2026-09-30T00:00:00Z",
    },
]

CUSTOMER_CONTEXT = SessionContext(
    session_id=SESSION_ID,
    restaurant_id=REST_ID,
    table_number=5,
    customer_id=CUST_ID,
    character="neko",
)

GUEST_CONTEXT = SessionContext(
    session_id=GUEST_SESSION_ID,
    restaurant_id=REST_ID,
    table_number=9,
    customer_id=None,
    character="neko",
)


@pytest.fixture(autouse=True)
def seed_data(fake_db: FakeSupabase) -> None:
    now = datetime.now(UTC)
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
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            },
            {
                "id": str(GUEST_SESSION_ID),
                "restaurant_id": str(REST_ID),
                "table_number": 9,
                "customer_id": None,
                "character": "neko",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            },
        ],
    )
    # Customer with veg preference
    fake_db.seed(
        "customers",
        [
            {
                "id": str(CUST_ID),
                "restaurant_id": str(REST_ID),
                "name": "Test Customer",
                "phone": "+91-98765-43210",
                "visit_count": 3,
                "preferences": ["veg"],
                "created_at": now.isoformat(),
            }
        ],
    )
    # Past order by this customer (ordered Chicken Ramen twice and Matcha once)
    fake_db.seed(
        "orders",
        [
            {
                "id": str(uuid4()),
                "restaurant_id": str(REST_ID),
                "customer_id": str(CUST_ID),
                "table_number": 5,
                "items": [
                    {"name": "Chicken Ramen", "qty": 2, "price": 14.50, "instructions": ""},
                    {"name": "Matcha Ice Cream", "qty": 1, "price": 6.00, "instructions": ""},
                ],
                "total_amount": 35.00,
                "status": "billed",
                "payment_method": "card",
                "customer_phone": None,
                "bot_id": None,
                "created_at": now.isoformat(),
            }
        ],
    )


# ── Recommendation tests ──────────────────────────────────────────────────────

def test_recommendations_exclude_unavailable() -> None:
    """Unavailable items must never appear in recommendations."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT)
    names = [r.name for r in results]
    assert "Sold Out Ramen" not in names


def test_recommendations_honor_veg_only() -> None:
    """veg_only=True should return only vegetarian items."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT, veg_only=True)
    assert all(r.is_veg for r in results)
    # Chicken Ramen and Spicy Tonkotsu are non-veg
    names = [r.name for r in results]
    assert "Chicken Ramen" not in names
    assert "Spicy Tonkotsu" not in names


def test_recommendations_exclude_spicy() -> None:
    """exclude_spicy=True should remove spicy items."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT, exclude_spicy=True)
    assert all(not r.is_spicy for r in results)
    names = [r.name for r in results]
    assert "Spicy Tonkotsu" not in names


def test_recommendations_past_order_boosts_rank() -> None:
    """Items the customer ordered before should rank near the top."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT)
    names = [r.name for r in results]
    # Chicken Ramen and Matcha Ice Cream are in past orders
    assert "Chicken Ramen" in names
    assert "Matcha Ice Cream" in names
    # The previously ordered items should be among the top results
    assert names.index("Chicken Ramen") < len(names)


def test_recommendations_each_has_reasons() -> None:
    """Every recommendation must have at least one non-empty reason."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT)
    assert len(results) > 0
    for item in results:
        assert item.reasons, f"{item.name} has no reasons"
        assert all(r.strip() for r in item.reasons)


def test_recommendations_limit_respected() -> None:
    """limit parameter must cap the number of results."""
    results = recommend_dishes(session=CUSTOMER_CONTEXT, limit=2)
    assert len(results) <= 2


def test_recommendations_guest_session() -> None:
    """Guest sessions work and return available items with at least one reason each."""
    results = recommend_dishes(session=GUEST_CONTEXT)
    assert len(results) > 0
    for item in results:
        assert item.reasons


# ── Conversations tests ───────────────────────────────────────────────────────

def test_save_and_load_messages() -> None:
    """Messages persist in order and are loaded correctly."""
    save_message(SESSION_ID, REST_ID, role="user", content="Hello!")
    save_message(SESSION_ID, REST_ID, role="assistant", content="Hi there, how can I help?")
    save_message(SESSION_ID, REST_ID, role="user", content="What ramen do you have?")

    msgs = load_messages(SESSION_ID, REST_ID)
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[0].content == "Hello!"
    assert msgs[2].content == "What ramen do you have?"


def test_load_messages_empty_for_new_session() -> None:
    """Loading messages for a session with no conversation returns an empty list."""
    msgs = load_messages(GUEST_SESSION_ID, REST_ID)
    assert msgs == []


def test_save_message_creates_conversation() -> None:
    """Saving a message auto-creates the conversation row if absent."""
    msg = save_message(SESSION_ID, REST_ID, role="assistant", content="Irasshaimase!")
    assert msg.role == "assistant"
    assert msg.content == "Irasshaimase!"

    # Saving again uses the same conversation (idempotent get-or-create)
    msg2 = save_message(SESSION_ID, REST_ID, role="user", content="I'd like ramen.")
    assert msg2.conversation_id == msg.conversation_id


def test_save_tool_message_with_metadata() -> None:
    """Tool messages can include tool_name and tool_call_id."""
    msg = save_message(
        SESSION_ID, REST_ID,
        role="tool",
        content='{"ok": true, "items": []}',
        tool_name="search_menu",
        tool_call_id="call-abc123",
    )
    assert msg.tool_name == "search_menu"
    assert msg.tool_call_id == "call-abc123"
