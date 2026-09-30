"""AI Orchestrator (Phase 3).

Handles:
  - System prompt generation with waiter personas and restaurant context
  - Customer MCP tool schema definitions for NVIDIA NIM (OpenAI format)
  - Tool execution loop bound to the session (max 5 iterations, 5s timeout)
  - SSE streaming: token events, draft events, done events
  - Conversation persistence (user, assistant, and tool messages)
  - Preservation of [emotion] tags and *actions*
"""
from __future__ import annotations

import asyncio
import json
import logging
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
    search_menu as svc_search_menu,
)
from maneki.services.orders import get_latest_active_order
from maneki.services.personas import build_system_prompt, get_menu_category_summary
from maneki.services.recommend import recommend_dishes as svc_recommend_dishes

logger = logging.getLogger("maneki.orchestrator")

MAX_TOOL_ITERATIONS = 5
TOOL_TIMEOUT_SECONDS = 5.0

# ── Customer MCP Tools in OpenAI Function Calling Format ──────────────────────

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


# ── Tool Execution Service ───────────────────────────────────────────────────

async def execute_tool_call(
    name: str,
    args: dict[str, Any],
    session: SessionContext,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    """Execute a customer MCP tool with strict session binding and timeout.

    Returns:
        (result_data, is_draft_mutating, updated_draft_dict_or_None)
    """
    is_mutation = name in DRAFT_MUTATING_TOOLS
    updated_draft: dict[str, Any] | None = None

    def _sync_exec() -> dict[str, Any]:
        nonlocal updated_draft
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
                res = svc_get_menu_item(restaurant_id=session.restaurant_id, name=item_name)
                if res.status == "found" and res.item:
                    return {"ok": True, "status": "found", "item": res.item.model_dump()}
                elif res.status == "ambiguous":
                    return {
                        "ok": True,
                        "status": "ambiguous",
                        "candidates": [c.model_dump() for c in res.candidates],
                        "message": f"Multiple items match '{item_name}'. Please specify which one.",
                    }
                else:
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
                draft = svc_add_item(
                    session_id=session.session_id,
                    restaurant_id=session.restaurant_id,
                    name=str(args.get("name", "")),
                    qty=int(args.get("qty", 1)),
                    instructions=str(args.get("instructions", "")),
                )
                formatted = format_draft(draft)
                updated_draft = formatted
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
                updated_draft = formatted
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
                updated_draft = formatted
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
                updated_draft = formatted
                return {
                    "ok": True,
                    "message": "Draft order cleared.",
                    "draft": formatted,
                }

            elif name == "get_order_status":
                status_res = get_latest_active_order(
                    session_id=session.session_id,
                    customer_id=session.customer_id,
                    restaurant_id=session.restaurant_id,
                    table_number=session.table_number,
                )
                return {"ok": True, **status_res}

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

    # Enforce 5 second timeout on all tool executions
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_exec),
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        result = {
            "ok": False,
            "error": "tool_timeout",
            "message": f"Tool '{name}' timed out after {TOOL_TIMEOUT_SECONDS}s.",
        }

    return result, is_mutation, updated_draft


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
      - `event: draft` -> when cart is modified (add/remove/qty/clear)
      - `event: token` -> text chunks of the assistant response
      - `event: done`  -> final completed response
      - `event: error` -> fatal errors or timeouts
    """
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

    # 1. Persist incoming user message
    try:
        save_message(
            session_id=sid,
            restaurant_id=rid,
            role="user",
            content=user_message,
        )
    except Exception as exc:
        logger.warning("Failed to save incoming user message: %s", exc)

    # 2. Check NVIDIA API key
    if not cfg.nvidia_api_key:
        yield _format_sse("error", {
            "ok": False,
            "error": "nvidia_key_missing",
            "message": "NVIDIA_API_KEY is not configured on the server.",
        })
        return

    # 3. Load customer profile & menu summary to build persona system prompt
    try:
        cust_profile = svc_get_customer_context(session_ctx)
    except Exception:
        cust_profile = {"guest": True, "name": "Guest"}

    try:
        cats = get_menu_category_summary(rid)
    except Exception:
        cats = []

    sys_prompt = build_system_prompt(
        character=character,
        customer_context=cust_profile,
        menu_categories=cats,
        table_number=table_number,
    )

    # 4. Load recent conversation history (last 20 messages)
    try:
        past_msgs = load_messages(session_id=sid, restaurant_id=rid, limit=20)
    except Exception:
        past_msgs = []

    # Build initial message chain
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

    # If the user message isn't already the last message in history, append it
    if not messages or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    # 5. NVIDIA Tool-calling loop (max 5 iterations)
    iteration = 0
    final_text_accumulated = ""

    async with httpx.AsyncClient(timeout=30.0) as client:
        while iteration < MAX_TOOL_ITERATIONS:
            iteration += 1

            payload = {
                "model": cfg.nvidia_model,
                "messages": messages,
                "tools": CUSTOMER_TOOLS,
                "tool_choice": "auto",
                "temperature": 0.2,
            }

            headers = {
                "Authorization": f"Bearer {cfg.nvidia_api_key}",
                "Content-Type": "application/json",
            }

            try:
                resp = await client.post(cfg.nvidia_endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.error("NVIDIA API call failed in iteration %d: %s", iteration, exc)
                yield _format_sse("error", {
                    "ok": False,
                    "error": "llm_error",
                    "message": f"AI service error: {exc}",
                })
                return

            choice = data["choices"][0]
            msg_obj = choice.get("message", {})
            tool_calls = msg_obj.get("tool_calls")
            content = msg_obj.get("content") or ""

            # Case A: LLM decided to execute tool calls
            if tool_calls:
                # Add assistant message with tool_calls to the context
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
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args = {}

                    logger.info("Executing tool %s with args: %s", fn_name, args)

                    tool_result, is_draft_mut, updated_draft = await execute_tool_call(
                        name=fn_name,
                        args=args,
                        session=session_ctx,
                    )

                    result_json = json.dumps(tool_result, default=str)

                    # If draft changed, emit draft SSE event immediately to update order panel
                    if is_draft_mut and updated_draft:
                        yield _format_sse("draft", {"draft": updated_draft})

                    # Persist tool message to conversation
                    try:
                        save_message(
                            session_id=sid,
                            restaurant_id=rid,
                            role="tool",
                            content=result_json,
                            tool_name=fn_name,
                            tool_call_id=tc_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to save tool message: %s", exc)

                    # Append tool response for next LLM iteration
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": result_json,
                    })

                # Proceed to next iteration so model sees tool outputs
                continue

            # Case B: LLM provided final conversational text (no further tool calls)
            final_text_accumulated = content
            break

    # If loop ended due to hitting max iterations without text output
    if not final_text_accumulated:
        final_text_accumulated = (
            "I've updated your table's items. Please check your order panel and press "
            "the Confirm button whenever you're ready! *smiles warmly*"
        )

    # 6. Stream tokens to SSE client
    # In token-based streaming we chunk the final text so the browser receives smooth tokens
    words = final_text_accumulated.split(" ")
    for idx, word in enumerate(words):
        token_piece = word + (" " if idx < len(words) - 1 else "")
        yield _format_sse("token", {"token": token_piece})
        # Micro yield to allow client to render smoothly
        await asyncio.sleep(0.01)

    # 7. Persist assistant final message
    try:
        save_message(
            session_id=sid,
            restaurant_id=rid,
            role="assistant",
            content=final_text_accumulated,
        )
    except Exception as exc:
        logger.warning("Failed to save assistant message: %s", exc)

    # 8. Emit done event
    yield _format_sse("done", {"message": final_text_accumulated})


def _format_sse(event: str, data: dict[str, Any]) -> str:
    """Format an SSE frame with event and JSON data."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
