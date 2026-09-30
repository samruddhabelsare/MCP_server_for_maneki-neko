# PRD — Maneki Neko MCP Server (Python)

**Owner:** Samruddha · **Status:** Ready to build · **Target agent:** Antigravity
**Repo name:** `maneki-mcp` · **Language:** Python 3.11+

---

## 0. How to use this PRD with Antigravity

1. Create an empty workspace, save this file as `docs/PRD.md`.
2. Put Section 14 ("Agent rules") into a workspace rule so it applies to every task.
3. First prompt: *"Read docs/PRD.md fully. Execute Milestone M0, then stop and show me the plan and results before M1."*
4. Proceed one milestone at a time. Each milestone has a Definition of Done (DoD) that must pass before moving on.

---

## 1. Summary

Maneki Neko is a multi-restaurant AI robot-waiter system. Today the **customer browser** (`app.js`) does everything: builds the LLM prompt, calls NVIDIA directly, parses `ORDER_UPDATE` / `ORDER_CONFIRM` text from the LLM, fixes prices, places orders in Supabase, polls status, and submits feedback. API keys live in client code.

**Build:** a Python service that becomes the single trusted business-logic layer:

- An **MCP server** exposing restaurant tools to the LLM (menu, draft order, recommendations, status).
- A separate **admin MCP scope** for kitchen/owner tools.
- A thin **REST backend** for non-LLM actions (login/session, confirm order, feedback, conversation load).
- (Phase 3) An **AI orchestrator** endpoint that runs the LLM tool-calling loop server-side and streams replies to the browser, so no API keys are ever in the browser.

## 2. Goals / Non-goals

**Goals**
1. LLM never manages cart JSON or prices. Tools validate against the DB and return canonical data.
2. Orders can only be placed by an explicit UI/backend action, never by LLM text.
3. Customer identity, restaurant, and table are bound server-side from the session, never supplied by the LLM.
4. Draft orders and conversations persist in Supabase (survive refresh, device switch, server restart).
5. Multi-restaurant isolation on every query.
6. Secrets only on the server.

**Non-goals**
- Voice (STT/TTS/ElevenLabs), avatar animation, emotion tags: stay in the browser.
- KDS / admin / bot-fleet UIs (they may call this service later).
- Payment gateway integration (QR payment stays a UI mock; `mark_billed` only records the method).
- Replacing Supabase.

## 3. Users

| User | Interacts via | Needs |
|---|---|---|
| Customer (dine-in) | Browser → backend → LLM → MCP tools | Chat/voice ordering that never mis-orders |
| Kitchen staff | Admin MCP / REST | Queue, status updates, mark items unavailable |
| Owner | Admin MCP (e.g. Claude Desktop) | Sales, feedback, price edits |
| Developer | Inspector, pytest | Testable tools, clear contracts |

## 4. Architecture

```
Browser ──REST/SSE──► FastAPI backend ──► AI orchestrator (Phase 3) ──► NVIDIA NIM
                            │                     │
                            │                     └── MCP tool calls (in-process or HTTP)
                            ▼                                   │
                     services/ (business logic) ◄───────────────┘
                            │
                            ▼
                        Supabase (service role)
```

- `services/` holds all business logic as plain Python functions. **MCP tools, REST routes, and the orchestrator all call the same services.** No logic is duplicated in tool handlers.
- Two MCP servers mounted in one ASGI app:
  - `/mcp/customer` → customer-scoped tools (session-bound)
  - `/mcp/admin` → admin/kitchen tools (admin-key protected)
- Transport: **Streamable HTTP**. (stdio optional for local Inspector testing via a flag.)

## 5. Tech stack

| Concern | Choice |
|---|---|
| MCP | Official `mcp` Python SDK (`FastMCP`), streamable HTTP |
| Web | FastAPI + Uvicorn; MCP apps mounted as sub-apps |
| DB client | `supabase` (supabase-py), service-role key |
| Validation | Pydantic v2, `pydantic-settings` for config |
| HTTP | `httpx` (async) for NVIDIA |
| Tests | `pytest`, `pytest-asyncio`, `respx` (mock HTTP) |
| Lint/format | `ruff`, `mypy` (strict on `services/`) |
| Packaging | `uv` + `pyproject.toml` |
| Container | Dockerfile (slim), `docker-compose.yml` for local |

Use async everywhere. Do not add dependencies beyond this list without asking.

## 6. Repo structure

```
maneki-mcp/
├─ pyproject.toml
├─ .env.example
├─ Dockerfile
├─ docs/PRD.md
├─ sql/
│  ├─ 001_drafts_conversations.sql      # already provided
│  └─ 002_confirm_draft_rpc.sql         # to be created (Section 9)
├─ src/maneki/
│  ├─ config.py                # pydantic-settings
│  ├─ db.py                    # supabase client singleton
│  ├─ models.py                # pydantic models (MenuItem, DraftItem, Draft, Session, Order…)
│  ├─ errors.py                # ManekiError hierarchy → mapped to MCP/REST errors
│  ├─ services/
│  │  ├─ sessions.py
│  │  ├─ customers.py
│  │  ├─ menu.py               # load, fuzzy match, search
│  │  ├─ drafts.py             # add/remove/set_qty/clear/get
│  │  ├─ orders.py             # confirm, status, transitions, list
│  │  ├─ recommend.py
│  │  ├─ conversations.py
│  │  ├─ feedback.py
│  │  ├─ analytics.py          # sales/feedback summaries
│  │  └─ personas.py           # 5 characters + rules text
│  ├─ mcp_customer.py          # FastMCP server, session-bound tools
│  ├─ mcp_admin.py             # FastMCP server, admin tools
│  ├─ api.py                   # FastAPI REST routes
│  ├─ orchestrator.py          # Phase 3: NVIDIA tool-calling loop + SSE
│  └─ main.py                  # ASGI app: mounts /mcp/customer, /mcp/admin, REST
└─ tests/
   ├─ conftest.py              # fake Supabase (in-memory) fixture
   ├─ test_menu_matching.py
   ├─ test_drafts.py
   ├─ test_orders.py
   ├─ test_session_binding.py
   └─ test_recommend.py
```

## 7. Configuration (`.env`)

```
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
ADMIN_API_KEY=                 # for /mcp/admin and admin REST
INTERNAL_MCP_TOKEN=            # orchestrator → /mcp/customer auth
NVIDIA_API_KEY=                # Phase 3
NVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
NVIDIA_ENDPOINT=https://integrate.api.nvidia.com/v1/chat/completions
MENU_TABLE=menu_items          # confirm real names against the live schema
ORDERS_TABLE=orders
CUSTOMERS_TABLE=customers
FEEDBACK_TABLE=feedback
CORS_ORIGINS=http://localhost:3000
SESSION_TTL_HOURS=6
LOG_LEVEL=INFO
```

Fail fast on startup if required vars are missing.

## 8. Domain rules (ported from current `app.js`, must be preserved)

1. **Phone normalization:** strip non-digits; drop leading `91` if 12 digits; if 10 digits → `+91-XXXXX-XXXXX`; otherwise return input unchanged.
2. **Quantity:** `qty` is exactly what the customer asked for. `(2pcs)` / `(4pcs)` in a menu name is serving size only and never changes qty. `add_item` **adds** to existing qty; `set_quantity` **sets** absolute qty; `qty = 0` via `set_quantity` removes the item.
3. **Price:** always from the menu table at call time. Never accepted from callers. Stored in the draft for display, but **re-read from the menu at confirm time** and totals recomputed.
4. **Menu matching:** exact (case-insensitive, trimmed) → prefix → contains. **Improvement over app.js:** if step 2 or 3 yields more than one candidate, do **not** pick the first. Return an `ambiguous` result listing candidates so the LLM can ask the customer.
5. **Availability:** `is_available = false` items cannot be added; return a clear error with the item name.
6. **Merge duplicates:** one row per canonical menu name per draft.
7. **Special requests** (`instructions`) are stored per draft line; empty string if none.
8. **Status lifecycle:** `pending → preparing → ready → delivered → billed`; `cancelled` allowed from `pending`, `preparing`, `ready`. Invalid transitions raise an error listing allowed next states.
9. **Multi-tenant:** every query filters by `restaurant_id` derived from the session (customer scope) or an explicit param (admin scope).
10. **Guests:** session with `customer_id = null`. History/recommendation tools degrade gracefully (popularity only).

## 9. Data model

`sql/001_drafts_conversations.sql` (already written) creates `sessions`, `order_drafts`, `conversations`, `messages`. Existing tables `menu_items`, `orders`, `customers`, `feedback` are used as-is; **agent must inspect the live schema (or ask) before assuming column names.** Expected columns:

- `menu_items`: `id, restaurant_id, name, category, price, is_veg, is_spicy, is_available`
- `orders`: `id, restaurant_id, table_number, customer_id, customer_phone, items(jsonb), total_amount, status, payment_method, bot_id, created_at`
- `customers`: `id, restaurant_id, phone, name, visit_count, preferences(text[]/jsonb)`
- `feedback`: `id, order_id, rating, comment, created_at`

**`sql/002_confirm_draft_rpc.sql`** (create): a Postgres function `confirm_draft(p_session_id uuid, p_items jsonb, p_total numeric)` that in **one transaction**:
1. locks the open draft for the session (`FOR UPDATE`), errors if none/empty,
2. inserts into `orders` (status `pending`, restaurant/table/customer from `sessions`),
3. sets draft `status='confirmed'`, `order_id=<new>`,
4. returns the new order row.

This makes confirm **atomic and idempotent** (double-click or retry cannot create two orders).

## 10. Customer MCP tools (`/mcp/customer`)

**Session binding:** every request carries header `X-Session-Id` and `Authorization: Bearer <INTERNAL_MCP_TOKEN>`. A shared helper `require_session(ctx)` resolves the session (must exist and be unexpired) and returns `restaurant_id, customer_id, table_number, character`. **No tool accepts `customer_id`, `phone`, `restaurant_id`, or `table_number` as an argument.** Missing/invalid → error, no fallback restaurant.

All tools return JSON with a stable shape: `{ "ok": true, ... }` or `{ "ok": false, "error": "<code>", "message": "...", ...details }`.

| Tool | Args | Behavior |
|---|---|---|
| `get_customer_context` | none | Name, preferences, visit_count, top-5 favourite items (from past orders). Guest → minimal. |
| `search_menu` | `query?`, `category?`, `veg_only?`, `spicy?`, `max_price?`, `limit=10` | Available items only. Filters combine with AND. |
| `get_menu_item` | `name` | Resolves via matching rules; returns item or `ambiguous` candidates or `not_found`. |
| `get_current_order` | none | Draft lines, per-line subtotal, total. |
| `add_item` | `name`, `qty>=1`, `instructions=""` | Adds qty; returns updated draft. Handles ambiguous / unavailable / not_found. |
| `remove_item` | `name` | Removes line; returns draft. |
| `set_quantity` | `name`, `qty>=0` | Sets absolute qty (0 removes). |
| `clear_order` | none | Empties draft. |
| `get_order_status` | none | Latest non-billed order for this session's customer/table: status, items, total, created_at. |
| `recommend_dishes` | `veg_only?`, `exclude_spicy?`, `limit=6` | Ranked candidates with `reasons` (ordered before / matches preference / popular). LLM phrases the final suggestion. |

**Explicitly NOT exposed to the LLM:** `confirm_order`, `submit_feedback`, `save_message`, `load_conversation`.

## 11. Admin MCP tools (`/mcp/admin`, header `X-Admin-Key`, explicit `restaurant_id` arg)

`list_orders(status?, table_number?, active_only?, limit)`, `get_order(order_id)`, `update_order_status(order_id, status)` (lifecycle enforced), `mark_billed(order_id, payment_method)`, `sales_summary(days)`, `feedback_summary(days)`, `set_item_availability(name, is_available)`, `update_item_price(name, price)`, `get_menu(include_unavailable)`.

Admin tools must never be reachable through `/mcp/customer` and vice versa.

## 12. REST API (non-LLM)

| Method / path | Purpose |
|---|---|
| `POST /sessions` | Body: `{restaurant_id, table_number, phone?, name?, preferences?, character?, guest?}`. Creates/updates customer (increments `visit_count` for existing), creates session. Returns `{session_id, customer, expires_at}`. Name required only for new customers. |
| `GET /sessions/{id}/draft` | Current draft + total (for the order panel). |
| `DELETE /sessions/{id}/draft/items/{name}` and `PATCH …` | Manual edits from the UI panel (✕ button, qty), via the same `drafts` service. |
| `POST /sessions/{id}/confirm` | **The only way to place an order.** Re-prices from DB, calls `confirm_draft` RPC, returns order. |
| `GET /orders/{id}/status` | Status for polling (replaces browser → Supabase polling). |
| `POST /orders/{id}/mark-billed` | Payment method; session-authorized. |
| `POST /orders/{id}/feedback` | rating 1–5, comment. |
| `GET /sessions/{id}/messages` | Load conversation for refresh/device switch. |
| `GET /sessions/{id}/history` | Customer's past orders (last 10). |
| `GET /healthz` | Liveness. |

Auth for session routes: `Authorization: Bearer <session_id>` and the session must be unexpired. Admin REST (if any) uses `X-Admin-Key`. CORS restricted to `CORS_ORIGINS`.

## 13. AI orchestrator (Phase 3)

`POST /sessions/{id}/chat` (SSE):
1. Load persona, customer context, conversation (last N messages), and menu category summary. Build system prompt = persona + rules + context. **Do not inline the full menu**; the model uses `search_menu`.
2. Call NVIDIA chat-completions with tool schemas generated from the customer MCP tools (OpenAI-style `tools`).
3. Tool loop: execute tool calls via services (session-bound), append results, repeat. **Max 5 iterations**, per-tool timeout 5 s.
4. Stream final text tokens as SSE `token` events; emit `draft` event (updated order panel data) after any draft-mutating tool; emit `done`.
5. Persist user + assistant messages via `conversations` service.
6. The reply text may contain `[emotion]` tags and `*actions*` as today. Server passes them through untouched (browser handles).
7. Persona rules included in the prompt: never invent items/prices; qty = exact number said; **never claim the order is placed**, tell the customer to press the Confirm button (or say "shall I get this ready for you to confirm?").

**Risk gate (must run in M6 before committing):** benchmark the configured model for tool-call reliability on ≥30 Hinglish/English ordering utterances (add, remove, "3 aur", ambiguous item, unavailable item, "kuch spicy suggest karo"). Report success rate. If the tool-call success rate is < 90 %, make the model configurable per-role (a stronger model for tool decisions) and document it.

## 14. Agent rules (paste as Antigravity workspace rule)

- Follow `docs/PRD.md` as the source of truth. If something is ambiguous or conflicts with the live DB schema, **ask** rather than guess.
- Business logic lives only in `services/`. MCP tools, REST routes, and the orchestrator are thin adapters.
- Never accept identity or restaurant scoping from LLM-controlled tool arguments (customer scope).
- Never log secrets, phone numbers in full (mask to last 4), or full prompts at INFO.
- Async everywhere; type-hint everything; `ruff` and `mypy` must pass.
- Every tool and route gets at least one test. Use an in-memory fake Supabase for unit tests; no network in tests.
- One milestone at a time. After each: run tests, summarize what changed, list assumptions, stop.
- Do not add dependencies outside Section 5 without approval.
- Keep tool descriptions short and precise: they are prompts for the LLM.

## 15. Milestones (each with Definition of Done)

**M0 · Scaffold**: `pyproject.toml`, `uv` env, config, logging, `db.py`, `/healthz`, Dockerfile, CI-style `make check` (ruff + mypy + pytest).
*DoD:* `uvicorn` starts, `/healthz` 200, `make check` green, startup fails clearly on missing env.

**M1 · Schema + read tools**: inspect live schema and report differences vs Section 9; apply `001`; write `002` RPC; `menu` + `customers` services; `search_menu`, `get_menu_item`, `get_customer_context` on customer MCP with session binding.
*DoD:* Inspector connects to `/mcp/customer` with headers; tools return correct data; calls without/with expired session are rejected; matching tests pass (exact/prefix/contains/ambiguous).

**M2 · Draft tools**: `drafts` service + `add_item`, `remove_item`, `set_quantity`, `clear_order`, `get_current_order`.
*DoD:* tests for: "6 gulab jamun" → qty 6 on an item named `Gulab Jamun (2pcs)`; add then add accumulates; set 0 removes; unavailable/unknown rejected; price ignores any caller value; one open draft per session under concurrent calls.

**M3 · Orders + REST**: `POST /sessions`, draft REST routes, `confirm` (RPC), status, mark-billed, feedback, history.
*DoD:* confirm twice → one order, second call returns the same order/clear error; total recomputed from current menu prices (test: change price between add and confirm); invalid status transition rejected; guest flow works.

**M4 · Conversations + recommend**: `conversations` service, `GET /messages`, `recommend_dishes`, `get_order_status`.
*DoD:* refresh restores chat; recommendations exclude unavailable and honor veg preference; each candidate has non-empty `reasons`.

**M5 · Admin MCP**: all Section 11 tools behind `X-Admin-Key`.
*DoD:* customer session cannot call admin tools and vice versa (tests); `sales_summary` numbers match a hand-computed fixture.

**M6 · Orchestrator** (gate in Section 13 first): SSE chat, tool loop, persistence, persona prompts.
*DoD:* end-to-end scripted conversation (search → add → change qty → ask status) works with the real model; benchmark report committed to `docs/toolcall-benchmark.md`; API key never reaches the client.

**M7 · Frontend cutover guide**: produce `docs/frontend-migration.md` listing exactly which parts of `app.js` to delete/replace (LLM calls, CORS proxies, `ORDER_UPDATE`/`ORDER_CONFIRM` parsing, confirm-phrase lists, direct Supabase calls) and the new calls to make. No frontend code changes unless asked.

## 16. Acceptance criteria (product level)

1. A customer can say "6 gulab jamun" and the draft shows qty 6 at the DB price.
2. Saying "yes place it" / "sure" / "ok" in chat **never** creates an order; only `POST /sessions/{id}/confirm` does.
3. Ordering a non-menu or unavailable item fails with a clear message and does not modify the draft.
4. Tool calls cannot read another customer's history or another restaurant's menu (test with forged args).
5. Refreshing the browser restores the draft and the conversation.
6. Double-clicking Confirm creates exactly one order.
7. No API key is present in any browser-delivered file.
8. `make check` passes; tool contracts documented in `README.md`.

## 17. Security & privacy

- Service-role key server-side only; RLS enabled on new tables with no public policies.
- Session IDs are UUIDv4, expire after `SESSION_TTL_HOURS`; expired sessions rejected everywhere.
- `INTERNAL_MCP_TOKEN` / `ADMIN_API_KEY` compared with constant-time compare.
- Treat LLM output and customer text as untrusted: all tool args validated by Pydantic; strings length-capped (e.g. `instructions` ≤ 200 chars, `query` ≤ 100).
- Rate limit `/chat` and `/sessions` per IP (simple in-memory limiter acceptable for v1).
- Mask phone numbers in logs.

## 18. Observability

Structured JSON logs with `session_id`, `tool`, `duration_ms`, `ok`. Add an `audit_log` write (best-effort) for `confirm`, status changes, price/availability edits (who = admin key label, what, when). Useful as evidence in the project report.

## 19. Open questions (agent must resolve in M1, not assume)

1. Exact column names/types in the live `menu_items`, `orders`, `customers`, `feedback` tables.
2. Whether `customers.preferences` is `text[]` or `jsonb`.
3. Whether phone is unique per restaurant or globally.
4. Whether `orders.items` already stores `instructions`.

## 20. Future (out of scope for v1)

Inventory/stockout agent tools, kitchen ETA and priority queue, bot-fleet tools (`assign_bot_persona`, `table_load`), loyalty points and coupons, churn detection, demand forecasting.
