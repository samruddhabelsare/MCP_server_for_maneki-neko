"""Integration tests for KDS and Admin REST endpoints.

Validates:
  - Header authentication: X-Admin-Key required on all routes.
  - Active orders endpoint for KDS (/admin/orders/active).
  - Status progression (/orders/{order_id}/status).
  - Analytics endpoints (/admin/analytics/sales, /admin/analytics/feedback).
  - Menu endpoints (/admin/menu, availability toggle, price update).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from maneki.config import get_settings
from maneki.main import app
from tests.conftest import FakeSupabase

REST_ID = uuid4()
NOW = datetime.now(UTC)

ITEM_1_ID = uuid4()
ITEM_2_ID = uuid4()

SAMPLE_MENU = [
    {
        "id": str(ITEM_1_ID),
        "restaurant_id": str(REST_ID),
        "name": "Miso Ramen",
        "category": "Mains",
        "price": 250.00,
        "is_veg": False,
        "is_spicy": True,
        "is_available": True,
        "allergens": ["soy"],
        "created_at": NOW.isoformat(),
    },
    {
        "id": str(ITEM_2_ID),
        "restaurant_id": str(REST_ID),
        "name": "Edamame",
        "category": "Starters",
        "price": 120.00,
        "is_veg": True,
        "is_spicy": False,
        "is_available": False,
        "allergens": ["soy"],
        "created_at": NOW.isoformat(),
    },
]

ORDER_PENDING_ID = uuid4()
ORDER_PREPARING_ID = uuid4()
ORDER_BILLED_ID = uuid4()

SAMPLE_ORDERS = [
    {
        "id": str(ORDER_PENDING_ID),
        "restaurant_id": str(REST_ID),
        "customer_id": None,
        "table_number": 3,
        "items": [
            {"name": "Miso Ramen", "qty": 2, "price": 250.00, "instructions": "Extra spicy"},
            {"name": "Edamame", "qty": 1, "price": 120.00, "instructions": ""},
        ],
        "total_amount": 620.00,
        "status": "pending",
        "payment_method": "cash",
        "customer_phone": None,
        "bot_id": None,
        "created_at": (NOW - timedelta(minutes=10)).isoformat(),
    },
    {
        "id": str(ORDER_PREPARING_ID),
        "restaurant_id": str(REST_ID),
        "customer_id": None,
        "table_number": 5,
        "items": [
            {"name": "Miso Ramen", "qty": 1, "price": 250.00, "instructions": ""},
        ],
        "total_amount": 250.00,
        "status": "preparing",
        "payment_method": "cash",
        "customer_phone": None,
        "bot_id": None,
        "created_at": (NOW - timedelta(minutes=5)).isoformat(),
    },
    {
        "id": str(ORDER_BILLED_ID),
        "restaurant_id": str(REST_ID),
        "customer_id": None,
        "table_number": 1,
        "items": [
            {"name": "Miso Ramen", "qty": 1, "price": 250.00, "instructions": ""},
        ],
        "total_amount": 250.00,
        "status": "billed",
        "payment_method": "card",
        "customer_phone": None,
        "bot_id": None,
        "created_at": (NOW - timedelta(hours=1)).isoformat(),
    },
]

SAMPLE_FEEDBACK = [
    {
        "id": str(uuid4()),
        "order_id": str(ORDER_BILLED_ID),
        "rating": 5,
        "comment": "Super tasty ramen!",
        "created_at": NOW.isoformat(),
    }
]


@pytest.fixture(autouse=True)
def seed_admin_data(fake_db: FakeSupabase) -> None:
    fake_db.seed("menu_items", SAMPLE_MENU)
    fake_db.seed("orders", SAMPLE_ORDERS)
    fake_db.seed("feedback", SAMPLE_FEEDBACK)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    cfg = get_settings()
    return {"X-Admin-Key": cfg.admin_api_key}


# ── Auth Tests ────────────────────────────────────────────────────────────────

def test_admin_routes_reject_missing_or_invalid_key(client: TestClient) -> None:
    # 1. Missing header
    res = client.get(f"/admin/orders/active?restaurant_id={REST_ID}")
    assert res.status_code == 401

    # 2. Invalid header
    res = client.get(
        f"/admin/orders/active?restaurant_id={REST_ID}",
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert res.status_code == 401


# ── KDS: Active Orders ────────────────────────────────────────────────────────

def test_get_active_orders_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.get(
        f"/admin/orders/active?restaurant_id={REST_ID}",
        headers=admin_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert "orders" in data
    orders = data["orders"]

    # Should include pending and preparing, but NOT billed
    order_ids = [o["id"] for o in orders]
    assert str(ORDER_PENDING_ID) in order_ids
    assert str(ORDER_PREPARING_ID) in order_ids
    assert str(ORDER_BILLED_ID) not in order_ids

    # Verify is_veg resolution
    pending_order = next(o for o in orders if o["id"] == str(ORDER_PENDING_ID))
    assert pending_order["table_number"] == 3
    items = pending_order["items"]
    assert len(items) == 2

    miso = next(i for i in items if i["name"] == "Miso Ramen")
    assert miso["is_veg"] is False
    assert miso["qty"] == 2
    assert miso["instructions"] == "Extra spicy"

    edamame = next(i for i in items if i["name"] == "Edamame")
    assert edamame["is_veg"] is True


# ── KDS: Advance Order Status ─────────────────────────────────────────────────

def test_update_order_status_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.patch(
        f"/orders/{ORDER_PENDING_ID}/status",
        headers=admin_headers,
        json={"status": "preparing"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["order_id"] == str(ORDER_PENDING_ID)
    assert data["status"] == "preparing"
    assert "updated_at" in data


def test_update_order_status_invalid_transition(client: TestClient, admin_headers: dict[str, str]) -> None:
    # Cannot jump pending -> delivered directly
    res = client.patch(
        f"/orders/{ORDER_PENDING_ID}/status",
        headers=admin_headers,
        json={"status": "delivered"},
    )
    assert res.status_code == 422


def test_update_order_status_not_found(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.patch(
        f"/orders/{uuid4()}/status",
        headers=admin_headers,
        json={"status": "preparing"},
    )
    assert res.status_code == 404


# ── Admin: Analytics Sales ────────────────────────────────────────────────────

def test_sales_analytics_endpoint(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.get(
        f"/admin/analytics/sales?restaurant_id={REST_ID}&range=7d",
        headers=admin_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert "total_revenue" in data
    assert "total_orders" in data
    assert "average_order_value" in data
    assert "top_items" in data
    assert "timeline" in data

    # Revenue only counts billed order (total 250.00)
    assert data["total_revenue"] == 250.00
    assert data["total_orders"] == 3
    assert data["average_order_value"] == 250.00

    # Top items
    top = data["top_items"]
    assert len(top) > 0
    miso_item = next(i for i in top if i["name"] == "Miso Ramen")
    # 2 in pending, 1 in preparing, 1 in billed = 4
    assert miso_item["sold"] == 4
    assert miso_item["revenue"] == 1000.00


# ── Admin: Analytics Feedback ─────────────────────────────────────────────────

def test_feedback_analytics_endpoint(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.get(
        f"/admin/analytics/feedback?restaurant_id={REST_ID}",
        headers=admin_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["average_rating"] == 5.0
    assert data["total_reviews"] == 1
    assert data["distribution"]["5"] == 1
    assert len(data["recent_comments"]) == 1
    comment = data["recent_comments"][0]
    assert comment["rating"] == 5
    assert comment["comment"] == "Super tasty ramen!"
    assert comment["order_id"] == str(ORDER_BILLED_ID)


# ── Admin: Menu Endpoints ─────────────────────────────────────────────────────

def test_get_admin_menu(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.get(
        f"/admin/menu?restaurant_id={REST_ID}",
        headers=admin_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    items = data["items"]
    # Admin menu includes unavailable items
    assert len(items) == 2
    edamame = next(i for i in items if i["name"] == "Edamame")
    assert edamame["is_available"] is False


def test_patch_item_availability(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.patch(
        f"/admin/menu/{ITEM_2_ID}/availability",
        headers=admin_headers,
        json={"is_available": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["item_id"] == str(ITEM_2_ID)
    assert data["is_available"] is True


def test_patch_item_price_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.patch(
        f"/admin/menu/{ITEM_1_ID}/price",
        headers=admin_headers,
        json={"price": 280.00},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["item_id"] == str(ITEM_1_ID)
    assert data["price"] == 280.00


def test_patch_item_price_validation_error(client: TestClient, admin_headers: dict[str, str]) -> None:
    res = client.patch(
        f"/admin/menu/{ITEM_1_ID}/price",
        headers=admin_headers,
        json={"price": -10.00},
    )
    assert res.status_code == 422
