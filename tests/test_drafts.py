"""Tests for draft order management (Milestone M2).

Verifies:
  - Serving size in name: '6 gulab jamun' on 'Gulab Jamun (2pcs)' sets qty=6.
  - Adding accumulates quantity.
  - set_quantity sets absolute qty.
  - set_quantity(0) removes the item.
  - remove_item removes the item.
  - Unavailable / unknown items are rejected.
  - Prices are strictly from the database.
  - clear_order empties the draft.
  - Instructions are stored and capped.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from maneki.mcp_customer import (
    add_item,
    clear_order,
    get_current_order,
    remove_item,
    set_quantity,
)
from maneki.models import SessionContext
from maneki.services.auth import current_session_ctx
from tests.conftest import FakeSupabase

REST_ID = uuid4()
CUST_ID = uuid4()
SESSION_ID = uuid4()

SAMPLE_MENU = [
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Gulab Jamun (2pcs)",
        "category": "Dessert",
        "price": 5.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": True,
        "allergens": ["dairy"],
        "created_at": "2026-09-30T00:00:00Z",
    },
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
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Truffle Fries",
        "category": "Side",
        "price": 10.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": False,  # Unavailable
        "allergens": [],
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
                "table_number": 4,
                "customer_id": str(CUST_ID),
                "character": "neko",
                "expires_at": (now + timedelta(hours=6)).isoformat(),
                "created_at": now.isoformat(),
            }
        ],
    )


@pytest.fixture(autouse=True)
def bind_session() -> None:
    token = current_session_ctx.set(
        SessionContext(
            session_id=SESSION_ID,
            restaurant_id=REST_ID,
            table_number=4,
            customer_id=CUST_ID,
            character="neko",
        )
    )
    yield
    current_session_ctx.reset(token)


@pytest.mark.asyncio
async def test_serving_size_in_name_does_not_affect_qty() -> None:
    """PRD Rule 2: '6 gulab jamun' -> qty 6 on 'Gulab Jamun (2pcs)'."""
    res = await add_item(name="Gulab Jamun", qty=6)
    assert res["ok"] is True
    draft = res["draft"]
    assert len(draft["items"]) == 1
    item = draft["items"][0]
    assert item["name"] == "Gulab Jamun (2pcs)"
    assert item["qty"] == 6
    assert item["price"] == 5.00
    assert item["subtotal"] == 30.00
    assert draft["total"] == 30.00


@pytest.mark.asyncio
async def test_add_accumulates_quantity() -> None:
    """Adding an item twice accumulates quantity."""
    await add_item(name="Chicken Ramen", qty=2)
    res = await add_item(name="Chicken Ramen", qty=3)

    assert res["ok"] is True
    draft = res["draft"]
    assert len(draft["items"]) == 1
    assert draft["items"][0]["qty"] == 5
    assert draft["total"] == round(5 * 14.50, 2)


@pytest.mark.asyncio
async def test_set_quantity_modifies_qty() -> None:
    """set_quantity sets the absolute quantity."""
    await add_item(name="Chicken Ramen", qty=2)
    res = await set_quantity(name="Chicken Ramen", qty=7)

    assert res["ok"] is True
    assert res["draft"]["items"][0]["qty"] == 7


@pytest.mark.asyncio
async def test_set_quantity_zero_removes_item() -> None:
    """set_quantity with qty=0 removes the item."""
    await add_item(name="Chicken Ramen", qty=2)
    res = await set_quantity(name="Chicken Ramen", qty=0)

    assert res["ok"] is True
    assert len(res["draft"]["items"]) == 0
    assert res["draft"]["total"] == 0.0


@pytest.mark.asyncio
async def test_remove_item() -> None:
    """remove_item removes line completely."""
    await add_item(name="Chicken Ramen", qty=2)
    await add_item(name="Gulab Jamun", qty=1)

    res = await remove_item(name="Chicken Ramen")
    assert res["ok"] is True
    assert len(res["draft"]["items"]) == 1
    assert res["draft"]["items"][0]["name"] == "Gulab Jamun (2pcs)"


@pytest.mark.asyncio
async def test_unavailable_item_rejected() -> None:
    """Unavailable items cannot be added."""
    res = await add_item(name="Truffle Fries", qty=1)
    assert res["ok"] is False
    assert res["error"] == "item_unavailable"


@pytest.mark.asyncio
async def test_unknown_item_rejected() -> None:
    """Non-existent items are rejected with item_not_found."""
    res = await add_item(name="Dragon Roll", qty=1)
    assert res["ok"] is False
    assert res["error"] == "item_not_found"


@pytest.mark.asyncio
async def test_clear_order() -> None:
    """clear_order empties all items from the draft."""
    await add_item(name="Chicken Ramen", qty=2)
    await add_item(name="Gulab Jamun", qty=4)

    res = await clear_order()
    assert res["ok"] is True
    assert len(res["draft"]["items"]) == 0
    assert res["draft"]["total"] == 0.0


@pytest.mark.asyncio
async def test_special_instructions_stored_and_capped() -> None:
    """Instructions are preserved and capped to 200 chars."""
    long_note = "extra spicy " * 30  # 360 chars
    res = await add_item(name="Chicken Ramen", qty=1, instructions=long_note)

    assert res["ok"] is True
    stored_note = res["draft"]["items"][0]["instructions"]
    assert len(stored_note) <= 200
    assert stored_note.startswith("extra spicy")


@pytest.mark.asyncio
async def test_get_current_order() -> None:
    """get_current_order returns the existing draft."""
    await add_item(name="Chicken Ramen", qty=1)
    res = await get_current_order()

    assert res["ok"] is True
    assert len(res["draft"]["items"]) == 1
    assert res["draft"]["items"][0]["name"] == "Chicken Ramen"
