"""Tests for menu loading, resolution (exact/prefix/contains/ambiguity), and search."""
from __future__ import annotations

from uuid import uuid4

import pytest

from maneki.errors import AmbiguousItemError, ItemNotFoundError
from maneki.services.menu import get_menu_item, search_menu
from tests.conftest import FakeSupabase

REST_ID = uuid4()
OTHER_REST_ID = uuid4()

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
        "allergens": ["gluten", "soy"],
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Spicy Miso Ramen",
        "category": "Main",
        "price": 16.00,
        "is_veg": False,
        "is_spicy": True,
        "is_available": True,
        "allergens": ["soy"],
        "created_at": "2026-09-30T00:00:00Z",
    },
    {
        "id": str(uuid4()),
        "restaurant_id": str(REST_ID),
        "name": "Veggie Gyoza (4pcs)",
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
        "name": "Pork Gyoza (4pcs)",
        "category": "Appetizer",
        "price": 9.00,
        "is_veg": False,
        "is_spicy": False,
        "is_available": False,  # Unavailable
        "allergens": ["gluten", "pork"],
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
        "restaurant_id": str(OTHER_REST_ID),
        "name": "Secret Dish",
        "category": "Main",
        "price": 99.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": True,
        "allergens": [],
        "created_at": "2026-09-30T00:00:00Z",
    },
]


@pytest.fixture(autouse=True)
def seed_menu(fake_db: FakeSupabase) -> None:
    fake_db.seed("menu_items", SAMPLE_MENU)


def test_exact_match() -> None:
    item = get_menu_item(REST_ID, "Chicken Ramen")
    assert item.name == "Chicken Ramen"
    assert item.price == 14.50


def test_case_insensitive_and_trimmed() -> None:
    item = get_menu_item(REST_ID, "   chicken ramen   ")
    assert item.name == "Chicken Ramen"


def test_prefix_match_unambiguous() -> None:
    item = get_menu_item(REST_ID, "Matcha")
    assert item.name == "Matcha Ice Cream"


def test_contains_match_unambiguous() -> None:
    item = get_menu_item(REST_ID, "Ice Cream")
    assert item.name == "Matcha Ice Cream"


def test_ambiguous_prefix_or_contains_raises() -> None:
    # "Ramen" matches "Chicken Ramen" and "Spicy Miso Ramen"
    with pytest.raises(AmbiguousItemError) as exc_info:
        get_menu_item(REST_ID, "Ramen")

    err = exc_info.value
    assert len(err.candidates) == 2
    assert "Chicken Ramen" in err.candidates
    assert "Spicy Miso Ramen" in err.candidates


def test_not_found_raises() -> None:
    with pytest.raises(ItemNotFoundError):
        get_menu_item(REST_ID, "California Roll")


def test_multi_restaurant_isolation() -> None:
    # REST_ID cannot see OTHER_REST_ID's items
    with pytest.raises(ItemNotFoundError):
        get_menu_item(REST_ID, "Secret Dish")

    other_item = get_menu_item(OTHER_REST_ID, "Secret Dish")
    assert other_item.name == "Secret Dish"


def test_search_menu_excludes_unavailable() -> None:
    items = search_menu(REST_ID)
    names = [i.name for i in items]
    assert "Pork Gyoza (4pcs)" not in names
    assert "Veggie Gyoza (4pcs)" in names


def test_search_menu_filters() -> None:
    # Veg only
    veg_items = search_menu(REST_ID, veg_only=True)
    assert all(i.is_veg for i in veg_items)
    assert len(veg_items) == 2  # Veggie Gyoza, Matcha Ice Cream

    # Spicy only
    spicy_items = search_menu(REST_ID, spicy=True)
    assert len(spicy_items) == 1
    assert spicy_items[0].name == "Spicy Miso Ramen"

    # Max price
    cheap_items = search_menu(REST_ID, max_price=10.0)
    assert all(i.price <= 10.0 for i in cheap_items)

    # Category
    desserts = search_menu(REST_ID, category="Dessert")
    assert len(desserts) == 1
    assert desserts[0].name == "Matcha Ice Cream"
