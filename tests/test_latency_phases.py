"""Tests for latency optimization phases (Phase 0 - Phase 5).

Verifies:
  - TTLCache hit/miss/expiry/stats/invalidation
  - Menu caching and cache invalidation on admin update
  - Customer profile caching
  - Prompt cache and menu snapshot in system prompt
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from maneki.cache import TTLCache, menu_cache, session_cache
from maneki.models import MenuItem
from maneki.services.menu import load_menu, update_item_price
from maneki.services.personas import build_system_prompt


def test_ttl_cache_basics() -> None:
    cache: TTLCache[str] = TTLCache(ttl=1, maxsize=5)
    cache.set("k1", "v1")
    assert cache.get("k1") == "v1"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 0

    assert cache.get("nonexistent") is None
    assert cache.stats()["misses"] == 1

    # Prefix invalidation
    cache.set("item:1", "a")
    cache.set("item:2", "b")
    cache.set("other:1", "c")
    cache.invalidate_prefix("item:")
    assert cache.get("item:1") is None
    assert cache.get("item:2") is None
    assert cache.get("other:1") == "c"


def test_ttl_cache_expiry() -> None:
    cache: TTLCache[str] = TTLCache(ttl=1, maxsize=5)
    cache.set("expiring", "val", ttl=0.01)
    time.sleep(0.02)
    assert cache.get("expiring") is None


def test_menu_cache_and_invalidation(fake_db: Any) -> None:
    rid = str(uuid4())
    item_id = str(uuid4())
    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Chicken Ramen",
                "price": 350.0,
                "is_veg": False,
                "is_spicy": False,
                "is_available": True,
                "category": "Ramen",
                "description": "Tasty",
            }
        ],
    )
    menu_cache.clear()

    # First load: cache miss
    m1 = load_menu(rid, include_unavailable=False)
    assert len(m1) == 1
    stats1 = menu_cache.stats()
    assert stats1["hits"] == 0
    assert stats1["misses"] == 1

    # Second load: cache hit
    m2 = load_menu(rid, include_unavailable=False)
    assert len(m2) == len(m1)
    stats2 = menu_cache.stats()
    assert stats2["hits"] == 1

    # Admin price update invalidates cache
    update_item_price(restaurant_id=rid, name="Chicken Ramen", price=999.0)
    stats3 = menu_cache.stats()
    assert stats3["size"] == 0  # invalidated

    # Reload: fetches updated price and repopulates cache
    m3 = load_menu(rid, include_unavailable=True)
    matching = [x for x in m3 if x.name == "Chicken Ramen"]
    assert matching[0].price == 999.0


def test_prompt_menu_snapshot() -> None:
    items = [
        MenuItem(
            id=uuid4(),
            restaurant_id=uuid4(),
            name="Test Ramen",
            price=300.0,
            is_veg=False,
            is_spicy=True,
            is_available=True,
            category="Ramen",
            description="Good ramen",
        )
    ]
    prompt = build_system_prompt(
        character="neko",
        customer_context=None,
        menu_categories=["Ramen"],
        table_number=1,
        menu_items=items,
    )
    assert "MENU SNAPSHOT" in prompt
    assert "Test Ramen" in prompt
    assert "300" in prompt


def test_ttl_cache_lru() -> None:
    cache: TTLCache[str] = TTLCache(ttl=10, maxsize=3)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.set("c", "3")
    assert len(cache) == 3

    # Accessing "a" moves it to end (most recently used)
    assert cache.get("a") == "1"

    # Adding "d" evicts the oldest entry ("b")
    cache.set("d", "4")
    assert len(cache) == 3
    assert cache.get("b") is None
    assert cache.get("a") == "1"
    assert cache.get("c") == "3"
    assert cache.get("d") == "4"


def test_menu_cache_invalidated_by_set_item_availability(fake_db: Any) -> None:
    rid = str(uuid4())
    item_id = str(uuid4())
    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Miso Soup",
                "price": 120.0,
                "is_veg": True,
                "is_spicy": False,
                "is_available": True,
                "category": "Soup",
                "description": "Warm miso",
            }
        ],
    )
    menu_cache.clear()

    # Pre-populate cache
    m1 = load_menu(rid, include_unavailable=False)
    assert len(m1) == 1
    assert menu_cache.get(f"menu:{rid}") is not None

    # Availability toggle via name invalidates menu cache
    from maneki.services.menu import set_item_availability
    set_item_availability(restaurant_id=rid, name="Miso Soup", is_available=False)
    assert menu_cache.get(f"menu:{rid}") is None

    # Re-cache
    load_menu(rid, include_unavailable=False)
    assert menu_cache.get(f"menu:{rid}") is not None

    # Availability toggle via ID invalidates menu cache
    from maneki.services.menu import set_item_availability_by_id
    set_item_availability_by_id(item_id=item_id, is_available=True)
    assert menu_cache.get(f"menu:{rid}") is None


def test_menu_cache_invalidated_by_update_item_price_by_id(fake_db: Any) -> None:
    rid = str(uuid4())
    item_id = str(uuid4())
    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Edamame",
                "price": 150.0,
                "is_veg": True,
                "is_spicy": False,
                "is_available": True,
                "category": "Sides",
                "description": "Steamed soybeans",
            }
        ],
    )
    menu_cache.clear()

    load_menu(rid, include_unavailable=False)
    assert menu_cache.get(f"menu:{rid}") is not None

    from maneki.services.menu import update_item_price_by_id
    update_item_price_by_id(item_id=item_id, price=180.0)
    assert menu_cache.get(f"menu:{rid}") is None


def test_confirm_order_uses_live_prices(fake_db: Any) -> None:
    """confirm_order must read live prices from the menu table, never from a cache."""
    rid = str(uuid4())
    sid = str(uuid4())
    item_id = str(uuid4())

    now = datetime.now(UTC)
    fake_db.seed(
        "sessions",
        [
            {
                "id": sid,
                "restaurant_id": rid,
                "table_number": 3,
                "customer_id": None,
                "character": "neko",
                "expires_at": (now + timedelta(hours=2)).isoformat(),
                "created_at": now.isoformat(),
            }
        ],
    )
    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Gyoza",
                "price": 200.0,
                "is_veg": False,
                "is_spicy": False,
                "is_available": True,
                "category": "Sides",
                "description": "Dumplings",
            }
        ],
    )

    from maneki.services.drafts import add_item
    add_item(session_id=sid, restaurant_id=rid, name="Gyoza", qty=2)

    # Directly change price in database (bypassing service invalidation)
    for row in fake_db._tables["menu_items"]:
        if row["id"] == item_id:
            row["price"] = 250.0

    from maneki.services.orders import confirm_order
    order = confirm_order(sid)
    # 2 * 250 = 500 (live price used, not original 200)
    assert order.total_amount == 500.0
    assert order.items[0].price == 250.0


def test_add_item_checks_live_availability(fake_db: Any) -> None:
    """add_item must validate dish against live menu lookup, not stale cache."""
    rid = str(uuid4())
    sid = str(uuid4())
    item_id = str(uuid4())

    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Bao Bun",
                "price": 180.0,
                "is_veg": True,
                "is_spicy": False,
                "is_available": True,
                "category": "Sides",
                "description": "Steamed buns",
            }
        ],
    )
    # Warm menu cache with item available
    load_menu(rid, include_unavailable=False)
    assert menu_cache.get(f"menu:{rid}") is not None

    # Directly mark item unavailable in database (simulating concurrent stockout)
    for row in fake_db._tables["menu_items"]:
        if row["id"] == item_id:
            row["is_available"] = False

    from maneki.errors import ItemUnavailableError
    from maneki.services.drafts import add_item

    with pytest.raises(ItemUnavailableError):
        add_item(session_id=sid, restaurant_id=rid, name="Bao Bun", qty=1)


def test_session_cache_reverifies_expiry(fake_db: Any) -> None:
    sid = str(uuid4())
    rid = str(uuid4())
    now = datetime.now(UTC)

    fake_db.seed(
        "sessions",
        [
            {
                "id": sid,
                "restaurant_id": rid,
                "table_number": 5,
                "customer_id": None,
                "character": "neko",
                "expires_at": (now + timedelta(seconds=2)).isoformat(),
                "created_at": now.isoformat(),
            }
        ],
    )
    session_cache.clear()

    from maneki.services.sessions import get_session
    s1 = get_session(sid)
    assert s1.id == UUID(sid)
    assert session_cache.get(f"session:{sid}") is not None

    # Mutate cached object to simulate time passage beyond expires_at
    cached_session = session_cache.get(f"session:{sid}")
    assert cached_session is not None
    # Set expires_at in the past
    cached_session.expires_at = now - timedelta(seconds=10)

    from maneki.errors import SessionExpiredError
    with pytest.raises(SessionExpiredError):
        get_session(sid)

    # Verify session was purged from cache
    assert session_cache.get(f"session:{sid}") is None


@pytest.mark.asyncio
async def test_nim_parser_resilience(respx_mock: Any) -> None:
    endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
    sse_body = (
        ": ping\n"
        "\n"
        "data: {\"choices\": [{\"delta\": {\"content\": \"Hello \"}}]}\n"
        "data: corrupted json line\n"
        "data:   {\"choices\": [{\"delta\": {\"content\": \"world!\"}}]}\n"
        "data: [DONE]\n"
    )
    respx_mock.post(endpoint).mock(return_value=httpx.Response(200, text=sse_body))

    from maneki.orchestrator import _stream_nim

    tokens: list[str] = []
    async with httpx.AsyncClient() as client:
        async for token, tool_calls in _stream_nim(
            client=client, payload={}, headers={}, nvidia_endpoint=endpoint
        ):
            if token:
                tokens.append(token)
            if tool_calls is not None:
                assert tool_calls == []

    # Verified spacing preserved exactly
    assert tokens == ["Hello ", "world!"]
    assert "".join(tokens) == "Hello world!"


@pytest.mark.asyncio
async def test_nim_parser_eof_without_done(respx_mock: Any) -> None:
    endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
    sse_body = (
        "data: {\"choices\": [{\"delta\": {\"content\": \"No sentinel stream\"}}]}\n"
    )
    respx_mock.post(endpoint).mock(return_value=httpx.Response(200, text=sse_body))

    from maneki.orchestrator import _stream_nim

    tokens: list[str] = []
    async with httpx.AsyncClient() as client:
        async for token, _ in _stream_nim(
            client=client, payload={}, headers={}, nvidia_endpoint=endpoint
        ):
            if token:
                tokens.append(token)

    assert "".join(tokens) == "No sentinel stream"


@pytest.mark.asyncio
async def test_nim_parser_tool_calls_stream(respx_mock: Any) -> None:
    endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
    sse_body = (
        "data: {\"choices\": [{\"delta\": {\"tool_calls\": [{\"index\": 0, \"id\": \"call_1\", \"function\": {\"name\": \"add_\"}}]}}]}\n"
        "data: {\"choices\": [{\"delta\": {\"tool_calls\": [{\"index\": 0, \"function\": {\"name\": \"item\", \"arguments\": \"{\\\"name\\\": \\\"Ramen\\\"}\"}}]}}]}\n"
        "data: [DONE]\n"
    )
    respx_mock.post(endpoint).mock(return_value=httpx.Response(200, text=sse_body))

    from maneki.orchestrator import _stream_nim

    final_tool_calls: list[dict[str, Any]] | None = None
    async with httpx.AsyncClient() as client:
        async for _, tool_calls in _stream_nim(
            client=client, payload={}, headers={}, nvidia_endpoint=endpoint
        ):
            if tool_calls is not None:
                final_tool_calls = tool_calls

    assert final_tool_calls is not None
    assert len(final_tool_calls) == 1
    assert final_tool_calls[0]["id"] == "call_1"
    assert final_tool_calls[0]["function"]["name"] == "add_item"
    assert final_tool_calls[0]["function"]["arguments"] == {"name": "Ramen"}


@pytest.mark.asyncio
async def test_concurrent_draft_mutations(fake_db: Any) -> None:
    """Concurrent mutating tool calls on the same session are serialized by session lock."""
    rid = str(uuid4())
    sid = str(uuid4())
    item_id = str(uuid4())

    fake_db.seed(
        "menu_items",
        [
            {
                "id": item_id,
                "restaurant_id": rid,
                "name": "Matcha Tea",
                "price": 80.0,
                "is_veg": True,
                "is_spicy": False,
                "is_available": True,
                "category": "Drinks",
                "description": "Green tea",
            }
        ],
    )

    from maneki.orchestrator import SessionContext, execute_tool_call
    session = SessionContext(
        session_id=UUID(sid),
        restaurant_id=UUID(rid),
        table_number=1,
        customer_id=None,
        character="neko",
    )

    async def _add() -> dict[str, Any]:
        result, is_mut, draft = await execute_tool_call(
            name="add_item",
            args={"name": "Matcha Tea", "qty": 1},
            session=session,
        )
        assert is_mut is True
        assert result.get("ok") is True
        return result

    # Execute 4 concurrent mutations on the same session
    results = await asyncio.gather(*[_add() for _ in range(4)])
    assert len(results) == 4

    from maneki.services.drafts import get_current_draft
    final_draft = get_current_draft(sid, rid)
    assert len(final_draft.items) == 1
    assert final_draft.items[0].qty == 4
