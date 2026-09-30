"""Tests for M6 AI Orchestrator and Personas.

Covers:
  - Waiter personas (5 characters) & system prompt construction
  - Customer MCP tool schemas (OpenAI format)
  - Tool execution dispatcher with session binding & draft mutations
  - Conversation persistence during chat turns
  - Scripted end-to-end conversation flow (search -> add -> change qty -> ask status)
  - SSE response streaming via FastAPI POST /sessions/{id}/chat
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from maneki.config import get_settings
from maneki.main import app
from maneki.models import Customer, SessionContext
from maneki.orchestrator import (
    CUSTOMER_TOOLS,
    execute_tool_call,
    stream_orchestrator_chat,
)
from maneki.services.conversations import load_messages
from maneki.services.drafts import get_current_draft
from maneki.services.personas import (
    DEFAULT_PERSONA,
    PERSONAS,
    build_system_prompt,
    get_persona,
)


@pytest.fixture
def test_data(fake_db: Any) -> dict[str, Any]:
    """Seed sample data for orchestrator tests."""
    rid = uuid4()
    cid = uuid4()
    sid = uuid4()

    fake_db.seed("restaurants", [{"id": str(rid), "name": "Neko Ramen Shibuya"}])
    fake_db.seed(
        "customers",
        [
            {
                "id": str(cid),
                "restaurant_id": str(rid),
                "phone": "+91-98765-43210",
                "name": "Tanaka",
                "visit_count": 5,
                "preferences": ["spicy", "extra broth"],
            }
        ],
    )
    fake_db.seed(
        "menu_items",
        [
            {
                "id": str(uuid4()),
                "restaurant_id": str(rid),
                "name": "Chicken Ramen",
                "category": "Ramen",
                "price": 350.0,
                "is_veg": False,
                "is_spicy": False,
                "is_available": True,
            },
            {
                "id": str(uuid4()),
                "restaurant_id": str(rid),
                "name": "Spicy Tonkotsu",
                "category": "Ramen",
                "price": 420.0,
                "is_veg": False,
                "is_spicy": True,
                "is_available": True,
            },
            {
                "id": str(uuid4()),
                "restaurant_id": str(rid),
                "name": "Matcha Ice Cream",
                "category": "Dessert",
                "price": 180.0,
                "is_veg": True,
                "is_spicy": False,
                "is_available": True,
            },
        ],
    )
    fake_db.seed(
        "sessions",
        [
            {
                "id": str(sid),
                "restaurant_id": str(rid),
                "table_number": 4,
                "customer_id": str(cid),
                "character": "neko",
                "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
                "created_at": datetime.now(UTC).isoformat(),
            }
        ],
    )

    return {"restaurant_id": rid, "customer_id": cid, "session_id": sid}


# ── 1. Personas & Prompts ─────────────────────────────────────────────────────

def test_all_five_personas_exist() -> None:
    expected = {"neko", "butler", "chef", "anime", "zen"}
    assert set(PERSONAS.keys()) == expected
    for pid in expected:
        p = get_persona(pid)
        assert p.id == pid
        assert p.name
        assert p.tone
        assert p.greeting
        assert p.style_guidance


def test_persona_fallback_to_default() -> None:
    p = get_persona("unknown_alien_character")
    assert p.id == DEFAULT_PERSONA
    p_none = get_persona(None)
    assert p_none.id == DEFAULT_PERSONA


def test_build_system_prompt_includes_rules_and_context() -> None:
    cust = Customer(
        id=uuid4(),
        restaurant_id=uuid4(),
        phone="+91-99999-88888",
        name="Aarav",
        visit_count=3,
        preferences=["spicy"],
    )
    prompt = build_system_prompt(
        character="butler",
        customer_context=cust,
        menu_categories=["Ramen", "Dessert"],
        table_number=5,
    )
    assert "Sebastian" in prompt
    assert "TABLE 5" in prompt
    assert "Aarav" in prompt
    assert "Ramen, Dessert" in prompt
    # Domain rules checks
    assert "NEVER guess or invent dish names" in prompt
    assert "NEVER CLAIM ORDER IS CONFIRMED" in prompt
    assert "Confirm Order" in prompt
    assert "QUANTITY ACCURACY" in prompt


def test_build_system_prompt_guest_customer() -> None:
    prompt = build_system_prompt(
        character="anime",
        customer_context={"guest": True, "name": "Guest"},
        menu_categories=["Appetizers"],
        table_number=2,
    )
    assert "Chibi Aoi" in prompt
    assert "Guest customer" in prompt
    assert "TABLE 2" in prompt


# ── 2. Tool Schemas ───────────────────────────────────────────────────────────

def test_customer_tools_openai_format() -> None:
    expected_names = {
        "get_customer_context",
        "search_menu",
        "get_menu_item",
        "get_current_order",
        "add_item",
        "remove_item",
        "set_quantity",
        "clear_order",
        "get_order_status",
        "recommend_dishes",
    }
    found_names = {t["function"]["name"] for t in CUSTOMER_TOOLS}
    assert found_names == expected_names
    for t in CUSTOMER_TOOLS:
        assert t["type"] == "function"
        assert t["function"]["description"]
        assert "parameters" in t["function"]


# ── 3. Tool Execution Dispatcher ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tool_call_add_and_mutate_draft(test_data: dict[str, Any]) -> None:
    ctx = SessionContext(
        session_id=test_data["session_id"],
        restaurant_id=test_data["restaurant_id"],
        table_number=4,
        customer_id=test_data["customer_id"],
        character="neko",
    )

    res, is_mut, updated = await execute_tool_call(
        name="add_item",
        args={"name": "Chicken Ramen", "qty": 2, "instructions": "extra hot"},
        session=ctx,
    )
    assert res["ok"] is True
    assert is_mut is True
    assert updated is not None
    assert updated["item_count"] == 2
    assert updated["total"] == 700.0


@pytest.mark.asyncio
async def test_execute_tool_call_search_menu(test_data: dict[str, Any]) -> None:
    ctx = SessionContext(
        session_id=test_data["session_id"],
        restaurant_id=test_data["restaurant_id"],
        table_number=4,
        customer_id=test_data["customer_id"],
        character="neko",
    )

    res, is_mut, updated = await execute_tool_call(
        name="search_menu",
        args={"query": "Ramen"},
        session=ctx,
    )
    assert res["ok"] is True
    assert is_mut is False
    assert updated is None
    assert res["count"] == 2


@pytest.mark.asyncio
async def test_execute_tool_call_unknown_tool(test_data: dict[str, Any]) -> None:
    ctx = SessionContext(
        session_id=test_data["session_id"],
        restaurant_id=test_data["restaurant_id"],
        table_number=4,
        customer_id=test_data["customer_id"],
        character="neko",
    )

    res, is_mut, updated = await execute_tool_call(
        name="hack_database",
        args={},
        session=ctx,
    )
    assert res["ok"] is False
    assert res["error"] == "unknown_tool"


# ── 4. Orchestrator Stream & Persistence ──────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_orchestrator_chat_missing_api_key(
    test_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    get_settings.cache_clear()

    events = []
    async for chunk in stream_orchestrator_chat(
        session_id=test_data["session_id"],
        restaurant_id=test_data["restaurant_id"],
        table_number=4,
        customer_id=test_data["customer_id"],
        character="neko",
        user_message="Hello!",
    ):
        events.append(chunk)

    combined = "".join(events)
    assert "event: error" in combined
    assert "nvidia_key_missing" in combined


@pytest.mark.asyncio
@respx.mock
async def test_scripted_end_to_end_conversation(
    test_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD Requirement: End-to-end scripted conversation with tool execution.

    Flow:
      Turn 1: Search menu -> LLM calls search_menu -> responds with options.
      Turn 2: Add item -> LLM calls add_item -> emits draft event -> responds.
      Turn 3: Change quantity -> LLM calls set_quantity -> emits draft event.
      Turn 4: Ask status -> LLM calls get_order_status -> responds with status.
    """
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-dummy-key")
    get_settings.cache_clear()

    cfg = get_settings()
    sid = test_data["session_id"]
    rid = test_data["restaurant_id"]
    cid = test_data["customer_id"]

    # ── Turn 1: Search Menu ──
    # Mock NVIDIA response 1: calls search_menu tool
    respx.post(cfg.nvidia_endpoint).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_search_1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_menu",
                                            "arguments": json.dumps({"query": "Ramen"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            # Model receives tool result and provides final text
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Nya~ We have Chicken Ramen (₹350) and Spicy Tonkotsu (₹420)!",
                            }
                        }
                    ]
                },
            ),
        ]
    )

    t1_events = []
    async for chunk in stream_orchestrator_chat(
        session_id=sid,
        restaurant_id=rid,
        table_number=4,
        customer_id=cid,
        character="neko",
        user_message="What ramen do you have?",
    ):
        t1_events.append(chunk)

    t1_combined = "".join(t1_events)
    assert "event: token" in t1_combined
    assert "event: done" in t1_combined
    assert "Chicken Ramen" in t1_combined

    # ── Turn 2: Add Item ──
    respx.post(cfg.nvidia_endpoint).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_add_1",
                                        "type": "function",
                                        "function": {
                                            "name": "add_item",
                                            "arguments": json.dumps({"name": "Chicken Ramen", "qty": 2}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I have added 2 Chicken Ramen to your draft order! Please confirm when ready.",
                            }
                        }
                    ]
                },
            ),
        ]
    )

    t2_events = []
    async for chunk in stream_orchestrator_chat(
        session_id=sid,
        restaurant_id=rid,
        table_number=4,
        customer_id=cid,
        character="neko",
        user_message="Add 2 Chicken Ramen",
    ):
        t2_events.append(chunk)

    t2_combined = "".join(t2_events)
    assert "event: draft" in t2_combined
    assert "Chicken Ramen" in t2_combined

    # Verify draft in DB
    draft = get_current_draft(sid, rid)
    assert len(draft.items) == 1
    assert draft.items[0].qty == 2

    # ── Turn 3: Change Quantity ──
    respx.post(cfg.nvidia_endpoint).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_qty_1",
                                        "type": "function",
                                        "function": {
                                            "name": "set_quantity",
                                            "arguments": json.dumps({"name": "Chicken Ramen", "qty": 1}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Updated! Chicken Ramen quantity is now set to 1.",
                            }
                        }
                    ]
                },
            ),
        ]
    )

    t3_events = []
    async for chunk in stream_orchestrator_chat(
        session_id=sid,
        restaurant_id=rid,
        table_number=4,
        customer_id=cid,
        character="neko",
        user_message="Change Chicken Ramen quantity to 1",
    ):
        t3_events.append(chunk)

    t3_combined = "".join(t3_events)
    assert "event: draft" in t3_combined
    draft = get_current_draft(sid, rid)
    assert draft.items[0].qty == 1

    # ── Turn 4: Check Order Status ──
    respx.post(cfg.nvidia_endpoint).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_status_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_order_status",
                                            "arguments": json.dumps({}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "You haven't confirmed your order yet! Press the Confirm button on your screen.",
                            }
                        }
                    ]
                },
            ),
        ]
    )

    t4_events = []
    async for chunk in stream_orchestrator_chat(
        session_id=sid,
        restaurant_id=rid,
        table_number=4,
        customer_id=cid,
        character="neko",
        user_message="What is my order status?",
    ):
        t4_events.append(chunk)

    t4_combined = "".join(t4_events)
    assert "event: done" in t4_combined
    assert "Confirm button" in t4_combined

    # ── Verify Conversation Persistence ──
    msgs = load_messages(session_id=sid, restaurant_id=rid, limit=50)
    assert len(msgs) >= 8  # 4 user turns + tool turns + assistant turns
    user_msgs = [m for m in msgs if m.role == "user"]
    assert len(user_msgs) == 4
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert len(tool_msgs) >= 4
    assistant_msgs = [m for m in msgs if m.role == "assistant"]
    assert len(assistant_msgs) == 4


# ── 5. REST API SSE Route ─────────────────────────────────────────────────────

def test_rest_chat_endpoint_sse(test_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    sid = test_data["session_id"]

    # When key is missing, SSE returns stream with error event
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    get_settings.cache_clear()

    resp = client.post(
        f"/sessions/{sid}/chat",
        json={"message": "Hello!"},
        headers={"Authorization": f"Bearer {sid}"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "nvidia_key_missing" in resp.text


def test_rest_chat_endpoint_invalid_session() -> None:
    client = TestClient(app)
    bad_sid = uuid4()
    resp = client.post(
        f"/sessions/{bad_sid}/chat",
        json={"message": "Hello"},
    )
    assert resp.status_code == 401
