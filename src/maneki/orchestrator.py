"""AI Orchestrator — latency-optimised, true SSE streaming.

Phases implemented:
  Phase 0: Structured timing logs (logger 'maneki.timing'), DEBUG_TIMING done event.
  Phase 1: Shared long-lived httpx.AsyncClient; true SSE from NIM (stream=True);
           delta.tool_calls accumulation; real token forwarding; retry before first byte.
  Phase 2: asyncio.to_thread for sync DB calls; concurrent setup with asyncio.gather;
           concurrent tool execution with per-session Lock; draft events after each mutation.
  Phase 3: Warm menu + categories from cache; profile from cache; compact menu in prompt.
  Phase 5: build_system_prompt receives menu_items for compact snapshot.

SSE contract (unchanged):
  event: token  {"token": "..."}
  event: draft  {"draft": {...}}
  event: done   {"message": "..."}   (+ "timing": {...} if DEBUG_TIMING=true)
  event: error  {"ok": false, "error": "...", "message": "..."}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import httpx

from maneki.config import get_settings
from maneki.errors import ManekiError
from maneki.models import SessionContext
from maneki.services.conversations import load_messages, save_message
from maneki.services.customers import get_customer_context as svc_get_customer_context
from maneki.services.drafts import (
    add_item as svc_add_item,
)
from maneki.services.drafts import (
    clear_draft as svc_clear_draft,
)
from maneki.services.drafts import (
    format_draft,
    get_current_draft,
)
from maneki.services.drafts import (
    remove_item as svc_remove_item,
)
from maneki.services.drafts import (
    set_quantity as svc_set_quantity,
)
from maneki.services.menu import (
    get_menu_item as svc_get_menu_item,
)
from maneki.services.menu import (
    load_menu,
    load_menu_categories,
)
from maneki.services.menu import (
    search_menu as svc_search_menu,
)
from maneki.services.orders import get_latest_active_order
from maneki.services.personas import build_system_prompt
from maneki.services.recommend import recommend_dishes as svc_recommend_dishes

logger = logging.getLogger("maneki.orchestrator")
timing_logger = logging.getLogger("maneki.timing")

MAX_TOOL_ITERATIONS = 5

# Per-session asyncio.Lock registry (Phase 2).
# Mutations on the same session draft are serialized; dict is cleaned lazily.
_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


# ── Customer MCP Tools in OpenAI Function Calling Format ─────────────────────

CUSTOMER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_customer_context",
            "description": "Retrieve customer profile, dining history, preferences, and top favorite dishes for this session.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_menu",
            "description": "Search available menu items by keyword, category, dietary preference (veg_only), spice level, or max price.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword or dish name.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category name filter (e.g. Ramen, Beverages, Desserts).",
                    },
                    "veg_only": {
                        "type": "boolean",
                        "description": "Filter strictly for vegetarian dishes.",
                    },
                    "spicy": {
                        "type": "boolean",
                        "description": "Filter strictly for spicy (true) or non-spicy (false) dishes.",
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Maximum price limit in rupees.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of items to return (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_menu_item",
            "description": "Look up a single menu item by name with exact, prefix, or contains matching. Returns item details or ambiguous candidate names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name or candidate phrase of the menu item.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_order",
            "description": "Get the current draft order (cart) for this session, showing all items, quantities, prices, and subtotal.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_item",
            "description": "Add an item to the session draft order. Increases quantity if already present. Validates availability and price from the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the dish to add.",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "Exact number of portions to add (must be at least 1).",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Special preparation instructions or dietary requests (e.g. 'extra spicy', 'no onions'). Max 200 chars.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "Remove an item line completely from the customer's draft order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the dish to remove.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_quantity",
            "description": "Set the absolute quantity of an item in the draft order. Setting qty=0 removes the item completely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the dish to update.",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "Absolute quantity (0 removes the item).",
                    },
                },
                "required": ["name", "qty"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_order",
            "description": "Empty the entire draft order cart.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Check the status of the latest non-billed order placed for this session (e.g. pending, preparing, ready, delivered).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_dishes",
            "description": "Get personalized dish recommendations ranked by customer history, preferences, and popularity, with reasons.",
            "parameters": {
                "type": "object",
                "properties": {
                    "veg_only": {
                        "type": "boolean",
                        "description": "Recommend only vegetarian options.",
                    },
                    "exclude_spicy": {
                        "type": "boolean",
                        "description": "Exclude spicy dishes from recommendations.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max recommendations to return (default 6).",
                    },
                },
            },
        },
    },
]

DRAFT_MUTATING_TOOLS = {"add_item", "remove_item", "set_quantity", "clear_order"}


# ── Tool Execution Service ────────────────────────────────────────────────────

async def execute_tool_call(
    name: str,
    args: dict[str, Any],
    session: SessionContext,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    """Execute a customer MCP tool with strict session binding and timeout.

    Phase 2: Sync DB calls wrapped in asyncio.to_thread.
    Draft-mutating tools hold the per-session Lock while running.

    Returns:
        (result_data, is_draft_mutating, updated_draft_dict_or_None)
    """
    is_mutation = name in DRAFT_MUTATING_TOOLS
    session_lock = _get_session_lock(str(session.session_id)) if is_mutation else None

    def _sync_exec() -> dict[str, Any]:
        nonlocal is_mutation
        updated_draft_holder: list[dict[str, Any] | None] = [None]

        try:
            if name == "get_customer_context":
                return {"ok": True, **svc_get_customer_context(session)}

            elif name == "search_menu":
                q = args.get("query")
                if q:
                    q = str(q)[:100]
                items = svc_search_menu(
                    restaurant_id=session.restaurant_id,
                    query=q,
                    category=args.get("category"),
                    veg_only=bool(args.get("veg_only", False)),
                    spicy=args.get("spicy"),
                    max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
                    limit=int(args.get("limit", 10)),
                )
                return {
                    "ok": True,
                    "count": len(items),
                    "items": [i.model_dump() for i in items],
                }

            elif name == "get_menu_item":
                item_name = str(args.get("name", ""))
                from maneki.errors import AmbiguousItemError, ItemNotFoundError  # noqa: PLC0415
                try:
                    item = svc_get_menu_item(restaurant_id=session.restaurant_id, name=item_name)
                    return {"ok": True, "status": "found", "item": item.model_dump()}
                except AmbiguousItemError as exc:
                    return {
                        "ok": True,
                        "status": "ambiguous",
                        "candidates": list(exc.candidates),
                        "message": f"Multiple items match '{item_name}'. Please specify which one.",
                    }
                except ItemNotFoundError:
                    return {
                        "ok": False,
                        "status": "not_found",
                        "error": "not_found",
                        "message": f"'{item_name}' was not found on the menu.",
                    }

            elif name == "get_current_order":
                draft = get_current_draft(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                )
                formatted = format_draft(draft)
                return {"ok": True, "draft": formatted}

            elif name == "add_item":
                added_draft = svc_add_item(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                    name=str(args.get("name", "")),
                    qty=int(args.get("qty", 1)),
                    instructions=str(args.get("instructions", "")),
                )
                formatted = format_draft(added_draft)
                updated_draft_holder[0] = formatted
                return {
                    "ok": True,
                    "message": "Added to draft order.",
                    "draft": formatted,
                }

            elif name == "remove_item":
                draft = svc_remove_item(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                    name=str(args.get("name", "")),
                )
                formatted = format_draft(draft)
                updated_draft_holder[0] = formatted
                return {
                    "ok": True,
                    "message": "Removed from draft order.",
                    "draft": formatted,
                }

            elif name == "set_quantity":
                draft = svc_set_quantity(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                    name=str(args.get("name", "")),
                    qty=int(args.get("qty", 0)),
                )
                formatted = format_draft(draft)
                updated_draft_holder[0] = formatted
                return {
                    "ok": True,
                    "message": "Updated quantity.",
                    "draft": formatted,
                }

            elif name == "clear_order":
                draft = svc_clear_draft(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                )
                formatted = format_draft(draft)
                updated_draft_holder[0] = formatted
                return {
                    "ok": True,
                    "message": "Draft order cleared.",
                    "draft": formatted,
                }

            elif name == "get_order_status":
                order = get_latest_active_order(
                    session_id=session.session_id,
                )
                if order is None:
                    return {"ok": True, "order": None, "message": "No active order found."}
                return {
                    "ok": True,
                    "order_id": str(order.id),
                    "status": order.status,
                    "table_number": order.table_number,
                    "items": [it.model_dump() for it in order.items],
                    "total_amount": order.total_amount,
                }

            elif name == "recommend_dishes":
                recs = svc_recommend_dishes(
                    restaurant_id=session.restaurant_id,
                    customer_id=session.customer_id,
                    veg_only=bool(args.get("veg_only", False)),
                    exclude_spicy=bool(args.get("exclude_spicy", False)),
                    limit=int(args.get("limit", 6)),
                )
                return {
                    "ok": True,
                    "count": len(recs),
                    "recommendations": [r.model_dump() for r in recs],
                }

            else:
                return {
                    "ok": False,
                    "error": "unknown_tool",
                    "message": f"Tool '{name}' is not recognized.",
                }

        except ManekiError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("Unexpected error in tool %s", name)
            return {"ok": False, "error": "internal_error", "message": str(exc)}

    # Run with lock (mutations) and timeout
    async def _run() -> tuple[dict[str, Any], dict[str, Any] | None]:
        result = await asyncio.to_thread(_sync_exec)
        updated_draft = result.get("draft") if name in DRAFT_MUTATING_TOOLS and result.get("ok") else None
        return result, updated_draft

    timeout_s = get_settings().tool_timeout_s
    try:
        if session_lock is not None:
            async with session_lock:
                result, updated_draft = await asyncio.wait_for(_run(), timeout=timeout_s)
        else:
            result, updated_draft = await asyncio.wait_for(_run(), timeout=timeout_s)
    except TimeoutError:
        result = {
            "ok": False,
            "error": "tool_timeout",
            "message": f"Tool '{name}' timed out after {timeout_s}s.",
        }
        updated_draft = None

    return result, is_mutation, updated_draft


# ── NIM Streaming Parser ──────────────────────────────────────────────────────

async def _stream_nim(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    headers: dict[str, str],
    nvidia_endpoint: str,
) -> AsyncGenerator[tuple[str, list[dict[str, Any]] | None], None]:
    """Stream from NIM and yield (text_delta, tool_calls_or_None) pairs.

    tool_calls_or_None is only set once, when the [DONE] sentinel arrives
    and tool_call chunks have been accumulated.

    Yields:
        ("text", None)         — for each text token chunk
        ("", tool_calls_list)  — once when tool calls complete (may be empty list)
    """
    # Accumulate partial tool_call arguments by index
    tool_call_acc: dict[int, dict[str, Any]] = {}
    raw_body_lines: list[str] = []
    got_sse_line = False

    done_sent = False

    async with client.stream("POST", nvidia_endpoint, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        async for raw_line in resp.aiter_lines():
            line = raw_line.strip()
            raw_body_lines.append(line)
            if not line or not line.startswith("data:"):
                continue
            got_sse_line = True
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                done_sent = True
                # Reconstruct tool_calls from accumulated chunks
                if tool_call_acc:
                    tool_calls: list[dict[str, Any]] = []
                    for idx in sorted(tool_call_acc.keys()):
                        tc = tool_call_acc[idx]
                        # json.loads the accumulated arguments string
                        raw_args = tc.get("function", {}).get("arguments", "{}")
                        try:
                            tc["function"]["arguments"] = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except Exception:
                            tc["function"]["arguments"] = {}
                        tool_calls.append(tc)
                    yield "", tool_calls
                else:
                    yield "", []
                return

            try:
                chunk = json.loads(data_str)
            except Exception:
                continue

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            # Text content
            content = delta.get("content")
            if content:
                yield content, None

            # Tool call deltas — accumulate by index
            delta_tcs = delta.get("tool_calls")
            if delta_tcs:
                for tc_delta in delta_tcs:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_call_acc:
                        tool_call_acc[idx] = {
                            "id": tc_delta.get("id", f"call_{idx}"),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    existing = tool_call_acc[idx]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        existing["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        existing["function"]["arguments"] += fn["arguments"]
                    if tc_delta.get("id"):
                        existing["id"] = tc_delta["id"]

    if got_sse_line and not done_sent:
        if tool_call_acc:
            tool_calls = []
            for idx in sorted(tool_call_acc.keys()):
                tc = tool_call_acc[idx]
                raw_args = tc.get("function", {}).get("arguments", "{}")
                try:
                    tc["function"]["arguments"] = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    tc["function"]["arguments"] = {}
                tool_calls.append(tc)
            yield "", tool_calls
        else:
            yield "", []
        return

    # ── Fallback: non-streaming JSON response (test mocks / non-SSE servers) ──
    if not got_sse_line:
        # Re-join body and parse as plain JSON
        body_text = "\n".join(raw_body_lines).strip()
        try:
            data = json.loads(body_text)
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
                raw_tool_calls = msg.get("tool_calls")
                if raw_tool_calls:
                    # Normalise arguments to dict
                    normalised: list[dict[str, Any]] = []
                    for tc in raw_tool_calls:
                        fn = tc.get("function", {})
                        raw_args = fn.get("arguments", "{}")
                        if isinstance(raw_args, str):
                            try:
                                fn["arguments"] = json.loads(raw_args)
                            except Exception:
                                fn["arguments"] = {}
                        normalised.append(tc)
                    if content:
                        yield content, None
                    yield "", normalised
                else:
                    if content:
                        yield content, None
                    yield "", []
        except Exception:
            yield "", []


# ── AI Orchestrator Core ──────────────────────────────────────────────────────

async def stream_orchestrator_chat(
    session_id: UUID | str,
    restaurant_id: UUID | str,
    table_number: int,
    customer_id: UUID | str | None,
    character: str,
    user_message: str,
) -> AsyncGenerator[str, None]:
    """Execute the AI waiter tool loop and stream SSE events to the browser.

    SSE Events:
      - event: draft  -> when cart is modified (add/remove/qty/clear)
      - event: token  -> text chunks of the assistant response
      - event: done   -> final completed response
      - event: error  -> fatal errors or timeouts
    """
    t_start = time.monotonic()
    sid = UUID(str(session_id))
    rid = UUID(str(restaurant_id))
    cid = UUID(str(customer_id)) if customer_id else None
    cfg = get_settings()

    session_ctx = SessionContext(
        session_id=sid,
        restaurant_id=rid,
        table_number=table_number,
        customer_id=cid,
        character=character,
    )

    # Phase 0 timing accumulators
    timing: dict[str, Any] = {}
    tool_timings: list[dict[str, Any]] = []

    # ── 1. Persist incoming user message (non-blocking) ──────────────────────
    try:
        await asyncio.to_thread(
            save_message,
            session_id=sid,
            restaurant_id=rid,
            role="user",
            content=user_message,
        )
    except Exception as exc:
        logger.warning("Failed to save incoming user message: %s", exc)

    # ── 2. Check NVIDIA API key ──────────────────────────────────────────────
    if not cfg.nvidia_api_key:
        yield _format_sse("error", {
            "ok": False,
            "error": "nvidia_key_missing",
            "message": "NVIDIA_API_KEY is not configured on the server.",
        })
        return

    # ── 3. Concurrent setup: profile + menu + history ────────────────────────
    t_setup_start = time.monotonic()

    async def _load_profile() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(svc_get_customer_context, session_ctx)
        except Exception:
            return {"guest": True, "name": "Guest"}

    async def _load_menu_items() -> list[Any]:
        try:
            return await asyncio.to_thread(load_menu, rid, False)
        except Exception:
            return []

    async def _load_categories() -> list[str]:
        try:
            return await asyncio.to_thread(load_menu_categories, rid)
        except Exception:
            return []

    async def _load_history() -> list[Any]:
        try:
            return await asyncio.to_thread(
                load_messages, sid, rid, cfg.history_messages
            )
        except Exception:
            return []

    cust_profile, menu_items, cats, past_msgs = await asyncio.gather(
        _load_profile(),
        _load_menu_items(),
        _load_categories(),
        _load_history(),
    )

    setup_ms = round((time.monotonic() - t_setup_start) * 1000, 1)
    timing["setup_ms"] = setup_ms

    # ── 4. Build system prompt (Phase 5 compact menu) ─────────────────────────
    sys_prompt = build_system_prompt(
        character=character,
        customer_context=cust_profile,
        menu_categories=cats,
        table_number=table_number,
        menu_items=menu_items,
    )

    # ── 5. Build message chain ────────────────────────────────────────────────
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
    for m in past_msgs:
        if m.role == "user":
            messages.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            messages.append({"role": "assistant", "content": m.content})
        elif m.role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": m.tool_call_id or "call_default",
                "name": m.tool_name or "tool",
                "content": m.content,
            })

    # Append user message if not already last
    if not messages or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    # ── 6. Get shared httpx client from app.state (Phase 1) ──────────────────
    # Import lazily to avoid circular dep at module level
    try:
        from maneki.main import app
        http_client: httpx.AsyncClient = app.state.http_client
    except Exception:
        # Fallback for tests that don't run lifespan
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20),
        )

    # ── 7. Tool-calling loop (max 5 iterations) ───────────────────────────────
    iteration = 0
    final_text_accumulated = ""
    nim_rounds = 0
    ttft_ms: float | None = None

    nim_headers = {
        "Authorization": f"Bearer {cfg.nvidia_api_key}",
        "Content-Type": "application/json",
    }

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1
        nim_rounds += 1
        t_nim_start = time.monotonic()

        payload: dict[str, Any] = {
            "model": cfg.nvidia_model,
            "messages": messages,
            "tools": CUSTOMER_TOOLS,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens": cfg.nim_max_tokens,
        }

        # ── True SSE streaming from NIM (Phase 1) ────────────────────────────
        if cfg.nim_stream:
            payload["stream"] = True
            first_byte_received = False
            text_in_this_round = ""
            tool_calls_in_this_round: list[dict[str, Any]] = []

            for attempt in range(2):
                try:
                    async for text_delta, tool_calls in _stream_nim(
                        client=http_client,
                        payload=payload,
                        headers=nim_headers,
                        nvidia_endpoint=cfg.nvidia_endpoint,
                    ):
                        if not first_byte_received:
                            first_byte_received = True
                            nim_first_byte_ms = round((time.monotonic() - t_nim_start) * 1000, 1)
                            timing["nim_first_byte_ms"] = nim_first_byte_ms

                        if tool_calls is not None:
                            # End of stream — got final tool_calls list
                            tool_calls_in_this_round = tool_calls
                            break

                        if text_delta:
                            # Forward text immediately to browser (Phase 1)
                            if ttft_ms is None:
                                ttft_ms = round((time.monotonic() - t_start) * 1000, 1)
                                timing["ttft_ms"] = ttft_ms
                            yield _format_sse("token", {"token": text_delta})
                            text_in_this_round += text_delta

                    # Success — break retry loop
                    break

                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if first_byte_received:
                        # Mid-stream failure: emit error and persist what we have
                        logger.error("NIM stream broke mid-response: %r", exc)
                        yield _format_sse("error", {
                            "ok": False,
                            "error": "llm_error",
                            "message": f"AI stream interrupted: {exc}",
                        })
                        if text_in_this_round:
                            with contextlib.suppress(Exception):
                                await asyncio.to_thread(
                                    save_message,
                                    session_id=sid,
                                    restaurant_id=rid,
                                    role="assistant",
                                    content=text_in_this_round,
                                )
                        return

                    # Pre-first-byte failure — retry once after 1s (Phase 1)
                    if attempt == 0:
                        logger.warning("NIM pre-first-byte failure (attempt 1): %r", exc)
                        await asyncio.sleep(1.0)
                        continue

                    logger.error("NIM API failed after retry: %r", exc, exc_info=True)
                    yield _format_sse("error", {
                        "ok": False,
                        "error": "llm_error",
                        "message": f"AI service timed out or connection failed: {exc}",
                    })
                    return

                except Exception as exc:
                    if first_byte_received:
                        logger.error("NIM stream error mid-response: %r", exc, exc_info=True)
                        yield _format_sse("error", {
                            "ok": False,
                            "error": "llm_error",
                            "message": f"AI stream error: {exc}",
                        })
                        return
                    logger.error("NIM API error: %r", exc, exc_info=True)
                    yield _format_sse("error", {
                        "ok": False,
                        "error": "llm_error",
                        "message": f"AI service error: {exc}",
                    })
                    return

            # Case A: tool calls requested this round
            if tool_calls_in_this_round:
                messages.append({
                    "role": "assistant",
                    "content": text_in_this_round or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": json.dumps(
                                    tc["function"]["arguments"]
                                    if isinstance(tc["function"]["arguments"], (dict, list))
                                    else {}
                                ),
                            },
                        }
                        for tc in tool_calls_in_this_round
                    ],
                })

                # Phase 2: Run independent tool calls concurrently
                # Draft-mutating tools are serialized via per-session lock inside execute_tool_call
                async def _exec_one(
                    tc: dict[str, Any], cur_iteration: int = iteration
                ) -> tuple[dict[str, Any], str, bool, dict[str, Any] | None]:
                    tc_id = tc.get("id", f"call_{cur_iteration}")
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    raw_args = fn.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            tc_args = json.loads(raw_args)
                        except Exception:
                            tc_args = {}
                    else:
                        tc_args = raw_args if isinstance(raw_args, dict) else {}

                    t_tool = time.monotonic()
                    logger.info("Executing tool %s with args: %s", fn_name, tc_args)
                    tool_result, is_draft_mut, updated_draft = await execute_tool_call(
                        name=fn_name,
                        args=tc_args,
                        session=session_ctx,
                    )
                    dur_ms = round((time.monotonic() - t_tool) * 1000, 1)
                    tool_timings.append({"tool": fn_name, "duration_ms": dur_ms})
                    return tool_result, tc_id, is_draft_mut, updated_draft

                tc_results = await asyncio.gather(
                    *[_exec_one(tc) for tc in tool_calls_in_this_round]
                )

                for tool_result, tc_id, is_draft_mut, updated_draft in tc_results:
                    # Emit draft SSE immediately (before next NIM round)
                    if is_draft_mut and updated_draft:
                        yield _format_sse("draft", {"draft": updated_draft})

                    result_json = json.dumps(tool_result, default=str)

                    try:
                        fn_name = next(
                            tc["function"]["name"]
                            for tc in tool_calls_in_this_round
                            if tc["id"] == tc_id
                        )
                    except StopIteration:
                        fn_name = "tool"

                    # Persist tool message
                    try:
                        await asyncio.to_thread(
                            save_message,
                            session_id=sid,
                            restaurant_id=rid,
                            role="tool",
                            content=result_json,
                            tool_name=fn_name,
                            tool_call_id=tc_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to save tool message: %s", exc)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": result_json,
                    })

                # Accumulate any text streamed alongside tool calls
                if text_in_this_round:
                    final_text_accumulated += text_in_this_round

                continue  # next iteration with tool results

            # Case B: pure text response (no tool calls)
            final_text_accumulated += text_in_this_round
            if ttft_ms is None and text_in_this_round:
                ttft_ms = round((time.monotonic() - t_nim_start) * 1000, 1)
                timing["ttft_ms"] = ttft_ms
            break

        else:
            # ── Non-streaming fallback (NIM_STREAM=false) ─────────────────
            resp_data: dict[str, Any] | None = None
            for attempt in range(2):
                try:
                    resp = await http_client.post(
                        cfg.nvidia_endpoint, json=payload, headers=nim_headers
                    )
                    resp.raise_for_status()
                    resp_data = resp.json()
                    if ttft_ms is None:
                        ttft_ms = round((time.monotonic() - t_nim_start) * 1000, 1)
                        timing["ttft_ms"] = ttft_ms
                    break
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    logger.warning("NVIDIA API attempt %d failed (transient): %r", attempt + 1, exc)
                    if attempt == 1:
                        logger.error("NVIDIA API failed after retries: %r", exc, exc_info=True)
                        yield _format_sse("error", {
                            "ok": False,
                            "error": "llm_error",
                            "message": f"AI service timed out or connection failed: {exc}",
                        })
                        return
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    logger.error("NVIDIA API error: %r", exc, exc_info=True)
                    yield _format_sse("error", {
                        "ok": False,
                        "error": "llm_error",
                        "message": f"AI service error: {exc}",
                    })
                    return

            if not resp_data:
                return

            choice = resp_data["choices"][0]
            msg_obj = choice.get("message", {})
            tool_calls = msg_obj.get("tool_calls")
            content = msg_obj.get("content") or ""

            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                })

                for tc in tool_calls:
                    tc_id = tc.get("id", f"call_{iteration}")
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    raw_args = fn.get("arguments", "{}")
                    try:
                        tc_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        tc_args = {}

                    logger.info("Executing tool %s with args: %s", fn_name, tc_args)
                    t_tool = time.monotonic()
                    tool_result, is_draft_mut, updated_draft = await execute_tool_call(
                        name=fn_name,
                        args=tc_args,
                        session=session_ctx,
                    )
                    tool_timings.append({"tool": fn_name, "duration_ms": round((time.monotonic() - t_tool) * 1000, 1)})

                    result_json = json.dumps(tool_result, default=str)

                    if is_draft_mut and updated_draft:
                        yield _format_sse("draft", {"draft": updated_draft})

                    try:
                        await asyncio.to_thread(
                            save_message,
                            session_id=sid,
                            restaurant_id=rid,
                            role="tool",
                            content=result_json,
                            tool_name=fn_name,
                            tool_call_id=tc_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to save tool message: %s", exc)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": result_json,
                    })
                continue

            # Final text
            final_text_accumulated = content
            # Stream word-by-word for non-streaming path (preserve original behaviour)
            words = final_text_accumulated.split(" ")
            for idx, word in enumerate(words):
                token_piece = word + (" " if idx < len(words) - 1 else "")
                yield _format_sse("token", {"token": token_piece})
                await asyncio.sleep(0.01)
            break

    # ── 8. Fallback message if loop exhausted ─────────────────────────────────
    if not final_text_accumulated:
        final_text_accumulated = (
            "I've updated your table's items. Please check your order panel and press "
            "the Confirm button whenever you're ready! *smiles warmly*"
        )
        words = final_text_accumulated.split(" ")
        for idx, word in enumerate(words):
            token_piece = word + (" " if idx < len(words) - 1 else "")
            yield _format_sse("token", {"token": token_piece})
            await asyncio.sleep(0.01)

    # ── 9. Persist assistant final message ───────────────────────────────────
    try:
        await asyncio.to_thread(
            save_message,
            session_id=sid,
            restaurant_id=rid,
            role="assistant",
            content=final_text_accumulated,
        )
    except Exception as exc:
        logger.warning("Failed to save assistant message: %s", exc)

    # ── 10. Emit done event (Phase 0 timing) ──────────────────────────────────
    total_ms = round((time.monotonic() - t_start) * 1000, 1)
    timing["total_ms"] = total_ms
    timing["nim_rounds"] = nim_rounds
    timing["tools"] = tool_timings

    # Collect cache stats
    from maneki.cache import menu_cache, profile_cache, session_cache  # noqa: PLC0415
    timing["cache"] = {
        "menu": menu_cache.stats(),
        "session": session_cache.stats(),
        "profile": profile_cache.stats(),
    }

    timing_logger.info(
        json.dumps({
            "session_id": str(sid),
            "restaurant_id": str(rid),
            **timing,
        })
    )

    done_data: dict[str, Any] = {"message": final_text_accumulated}
    if cfg.debug_timing:
        done_data["timing"] = timing

    yield _format_sse("done", done_data)


def _format_sse(event: str, data: dict[str, Any]) -> str:
    """Format an SSE frame with event and JSON data."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
