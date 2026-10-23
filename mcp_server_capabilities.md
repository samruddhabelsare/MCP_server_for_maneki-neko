# Maneki Neko MCP Server — Full Capabilities Reference

**Live URL:** `https://mcp-server-for-maneki-neko.onrender.com`  
**Docs UI:** `https://mcp-server-for-maneki-neko.onrender.com/docs`  
**Protocol Version:** MCP 1.0 (Model Context Protocol — Streamable HTTP Transport) & OpenAPI / REST

---

## 1. Architecture Overview

The Maneki Neko backend operates across **two distinct architectural layers** designed for high throughput, sub-second AI conversational response, and isolated admin controls:

| Layer | Protocol & Transport | Purpose | Primary Consumers |
| :--- | :--- | :--- | :--- |
| **REST API** | HTTP / JSON & Server-Sent Events (SSE) | Customer dining session lifecycle, cart manual controls, real-time AI waiter streaming chat, Kitchen Display System (KDS), and Admin Dashboard | Customer web frontend (`app.js`), table tablets, kitchen displays, manager dashboards |
| **MCP Tools** | JSON-RPC 2.0 / Streamable HTTP (`/mcp/customer` & `/mcp/admin`) | Model context tools executed autonomously during agentic reasoning, kitchen operations, and restaurant administration | AI Orchestrator (NVIDIA NIM), MCP Inspector, external MCP clients |

```
                       [Customer Web / Tablet UI]         [Kitchen Display & Admin Dashboard]
                                   │                                       │
            POST /sessions/{id}/chat (SSE Stream)                          │
            POST /sessions/{id}/confirm                                    │ REST Endpoints
            PATCH /sessions/{id}/draft/items/{name}                        │ (X-Admin-Key)
                                   │                                       │
                                   ▼                                       ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ FastAPI Application Gateway                                                            │
│                                                                                        │
│  ├─ REST Routers:                                                                      │
│  │    ├─ Customer Router (api.py): Sessions, Draft Cart, Orders, Chat, History, Feedback│
│  │    └─ Admin & KDS Router (admin_api.py): Active KDS Orders, Status Updates, Analytics│
│  │                                                                                     │
│  ├─ Shared Async Resources & Lifecycle:                                                │
│  │    ├─ Persistent keep-alive httpx.AsyncClient (HTTP/2 enabled when h2 installed)    │
│  │    ├─ FastMCP Streamable HTTP Session Managers running in FastAPI lifespan         │
│  │    └─ Cache pre-warming on startup for all active restaurants                       │
│  │                                                                                     │
│  ├─ Multi-Tier In-Memory TTLCache (Thread-safe, Monotonic Clock, LRU Eviction):        │
│  │    ├─ Menu Catalog & Categories: 45s TTL (busted on availability/price changes)    │
│  │    ├─ Sessions: 30s TTL (re-checks expires_at against UTC now on every hit)         │
│  │    ├─ Customer Profiles & Favorites: 120s TTL (busted on order confirmation)       │
│  │    ├─ Dish Popularity Scores: 300s TTL                                             │
│  │    └─ System Prompt Static Prefixes: 3600s TTL (busted on menu/character change)   │
│  │                                                                                     │
│  └─ Per-Session Async Mutex (asyncio.Lock) serializing concurrent draft modifications   │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
                    ┌──────────────────────┴──────────────────────┐
                    ▼                                             ▼
┌──────────────────────────────────────────────┐ ┌────────────────────────────────────────┐
│ Customer MCP Server (/mcp/customer)          │ │ Admin MCP Server (/mcp/admin)          │
│ Bound to active table session                │ │ Protected by X-Admin-Key               │
│ Auth: X-Session-Id + Bearer Token            │ │ Auth: X-Admin-Key header / parameter   │
│                                              │ │                                        │
│  10 Autonomous Customer Tools:               │ │  9 Autonomous Admin / Kitchen Tools:   │
│  ├─ get_customer_context                     │ │  ├─ list_orders                        │
│  ├─ search_menu                              │ │  ├─ get_order                          │
│  ├─ get_menu_item                            │ │  ├─ update_order_status                │
│  ├─ get_current_order                        │ │  ├─ mark_billed                        │
│  ├─ add_item                                 │ │  ├─ sales_summary                      │
│  ├─ remove_item                              │ │  ├─ feedback_summary                   │
│  ├─ set_quantity                             │ │  ├─ set_item_availability              │
│  ├─ clear_order                              │ │  ├─ update_item_price                  │
│  ├─ get_order_status                         │ │  └─ get_menu                           │
│  └─ recommend_dishes                         │ └───────────────────┬────────────────────┘
└──────────────────────┬───────────────────────┘                     │
                       │                                             │
                       ▼                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ Supabase / PostgreSQL Engine & RPC Functions                                           │
│                                                                                        │
│  ├─ draft_apply(p_session_id, p_restaurant_id, p_op, p_name, p_qty, p_price, p_inst)   │
│  │    └─ SELECT ... FOR UPDATE atomic row-level lock (add / set / remove / clear)      │
│  ├─ confirm_draft(p_session_id, p_items, p_total)                                      │
│  │    └─ Atomic order creation & total recalculation from live menu_items table        │
│  └─ Relational Tables: restaurants, menu_items, customers, sessions, order_drafts,     │
│                        orders, feedback, conversations, messages                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Real-Time Streaming & Latency Capabilities

The AI orchestrator and server architecture are engineered specifically for **sub-1-second Time-To-First-Token (TTFT)** and real-time frontend synchronization:

- **True Server-Sent Events (SSE)**: Direct token piping (`event: token`) from NVIDIA NIM to the client without buffering, preserving exact whitespace and punctuation.
- **Thinking / CoT Suppression**: Enforces `chat_template_kwargs: {"thinking": False}` on NVIDIA Nemotron models, completely eliminating internal reasoning dumps and immediately outputting conversational speech.
- **Pre-Tool Conversational Acknowledgment**: Persona prompting instructs the model to stream an immediate single acknowledgement sentence (e.g., *"Ek second, check karta hoon..."* or *"Let me check that for you right away!"*) before firing tool calls. The customer receives instant visual feedback while tool execution takes place.
- **Compact Menu Snapshot in Prompt**: When a restaurant has $\le$ `MENU_IN_PROMPT_MAX` (default 60) available dishes, a formatted snapshot is injected into the system prompt. The model answers menu queries and provides recommendations directly without tool call overhead. Order mutations (`add_item`, `set_quantity`, `remove_item`) still invoke tools for transactional consistency.
- **Atomic Single-Roundtrip Draft RPC (`draft_apply`)**: Postgres RPC executes row-level `SELECT ... FOR UPDATE` locks on the open draft row, serializing additions and modifications in a single database round trip and preventing lost-update race conditions.
- **Immediate Draft Broadcasts**: Whenever a tool mutates a cart, an `event: draft` is emitted into the SSE stream immediately prior to subsequent model generation rounds.
- **Concurrent Context Gathering**: Uses `asyncio.gather` and `asyncio.to_thread` to fetch customer profiles, menu categories, and message histories concurrently.
- **Connection Keep-Alive & HTTP/2**: Persistent `httpx.AsyncClient` with pooled keep-alive connections to NVIDIA NIM and Supabase, supporting HTTP/2 multiplexing when the `h2` library is available.

---

## 3. REST API Reference

### 🔐 Authentication Patterns

| Area | Header Requirement | Description |
| :--- | :--- | :--- |
| **Customer Endpoints** | Optional: `Authorization: Bearer <session_id>` | Validates that caller has access to the requested dining session. |
| **KDS & Admin Endpoints** | Required: `X-Admin-Key: <ADMIN_API_KEY>` | Protects all kitchen display, order status updates, analytics, and menu management routes. |

---

### 3.1 Operations & Health Checks

#### Health & Readiness Check
```http
GET /healthz
```
- **Response (200 OK)**:
  ```json
  {
    "status": "ok",
    "uptime_seconds": 1420.5,
    "supabase": "ok",
    "version": "0.1.0"
  }
  ```
- **Degraded Response (503 Service Unavailable)**: Returned if database connectivity fails.

---

### 3.2 Customer Dining Session Management

#### Create Dining Session
```http
POST /sessions
Content-Type: application/json

{
  "restaurant_id": "e709b814-b895-4c2a-bec2-dad69019ccde",
  "table_number": 3,
  "phone": "+919876543210",    // Optional: auto-normalized to +91-XXXXX-XXXXX
  "name": "Riya",              // Optional: customer name
  "preferences": ["spicy"],    // Optional: dietary tags (e.g. "veg", "spicy")
  "character": "neko",         // Persona: "neko" | "butler" | "chef" | "anime" | "zen"
  "guest": false               // true = anonymous session, no profile saved
}
```
- **Behavior**:
  - If `phone` is provided, normalizes phone number and upserts into `customers` table, automatically incrementing `visit_count` on return visits.
  - If `guest: true`, customer profile linking is bypassed.
- **Response**:
  ```json
  {
    "ok": true,
    "session_id": "923b17bd-a33c-4c9e-a322-02115353bffe",
    "restaurant_id": "e709b814-b895-4c2a-bec2-dad69019ccde",
    "table_number": 3,
    "customer": {
      "id": "c1a2b3c4-...",
      "name": "Riya",
      "phone": "+91-98765-43210",
      "visit_count": 2,
      "preferences": ["spicy"]
    },
    "character": "neko",
    "expires_at": "2026-10-23T21:30:00Z"
  }
  ```

#### Get Conversation History (Page Refresh / Tab Restoration)
```http
GET /sessions/{id}/messages
Authorization: Bearer <session_id>
```
- **Response**: Last 50 messages in chronological order.
  ```json
  {
    "ok": true,
    "messages": [
      {
        "id": "...",
        "conversation_id": "...",
        "role": "user",
        "content": "Can I get 2 Miso Ramens?",
        "tool_name": null,
        "tool_call_id": null,
        "created_at": "2026-10-23T15:00:00Z"
      },
      {
        "id": "...",
        "conversation_id": "...",
        "role": "assistant",
        "content": "Ek second, check karta hoon... *waves paw* [happy] I have added 2 Miso Ramens to your draft order!",
        "tool_name": null,
        "tool_call_id": null,
        "created_at": "2026-10-23T15:00:02Z"
      }
    ]
  }
  ```

#### Get Customer Past Order History
```http
GET /sessions/{id}/history
Authorization: Bearer <session_id>
```
- **Response**: Up to 10 past confirmed orders for the authenticated customer profile.

---

### 3.3 Streaming AI Waiter Chat

```http
POST /sessions/{id}/chat
Authorization: Bearer <session_id>
Content-Type: application/json

{
  "message": "Ek spicy miso ramen aur do gyoza add kardo"
}
```

**SSE Event Protocol (`text/event-stream`):**

| Event Type | Payload Format | Description |
| :--- | :--- | :--- |
| `event: token` | `{"token": "Ek"}` | Emitted token-by-token as generated by NVIDIA NIM. Exact spacing preserved. |
| `event: draft` | `{"draft": {"draft_id": "...", "items": [...], "total": 520.0, "item_count": 3}}` | Broadcast immediately whenever a cart mutating tool executes. |
| `event: done` | `{"message": "...", "timing": {...}}` | Marks stream completion with full message text and optional latency metrics. |
| `event: error` | `{"ok": false, "error": "...", "message": "..."}` | Emitted on unrecoverable generation, inference, or timeout exceptions. |

**Timing Breakdown in `event: done` (when `DEBUG_TIMING=true`):**
```json
{
  "message": "I've added 1 Spicy Miso Ramen and 2 Gyoza to your draft order!",
  "timing": {
    "setup_ms": 12.4,
    "ttft_ms": 385.0,
    "nim_first_byte_ms": 340.2,
    "tools": [
      { "tool": "add_item", "duration_ms": 38.6 },
      { "tool": "add_item", "duration_ms": 32.1 }
    ],
    "cache": {
      "menu": { "hits": 5, "misses": 0, "hit_rate": 1.0, "size": 1 },
      "session": { "hits": 4, "misses": 0, "hit_rate": 1.0, "size": 1 }
    }
  }
}
```

---

### 3.4 Manual Draft Panel Operations (UI +/- & Remove Buttons)

#### View Current Open Draft
```http
GET /sessions/{id}/draft
Authorization: Bearer <session_id>
```
- **Response**:
  ```json
  {
    "ok": true,
    "draft": {
      "draft_id": "11223344-...",
      "items": [
        {
          "name": "Miso Ramen",
          "qty": 2,
          "price": 250.0,
          "subtotal": 500.0,
          "instructions": "Extra spicy"
        }
      ],
      "total": 500.0,
      "item_count": 2
    }
  }
  ```

#### Set Item Quantity (UI +/- Buttons)
```http
PATCH /sessions/{id}/draft/items/{name}
Authorization: Bearer <session_id>
Content-Type: application/json

{ "qty": 3 }
```
- *Note:* Setting `qty: 0` automatically removes the item from the draft.

#### Remove Item Completely (UI ✕ Button)
```http
DELETE /sessions/{id}/draft/items/{name}
Authorization: Bearer <session_id>
```

---

### 3.5 Order Placement, Polling & Billing

#### Confirm Order ("Place Order" Button)
```http
POST /sessions/{id}/confirm
Authorization: Bearer <session_id>
```
- **Execution**: Atomically executes Postgres `confirm_draft` RPC.
- **Safety**: Re-reads prices from the live `menu_items` table and recalculates total.
- **Idempotency**: Repeated calls return the already placed order without duplicate placement.
- **Response**:
  ```json
  {
    "ok": true,
    "order": {
      "id": "e6a1f33b-...",
      "restaurant_id": "...",
      "customer_id": "...",
      "table_number": 3,
      "items": [
        { "name": "Miso Ramen", "qty": 2, "price": 250.0, "instructions": "Extra spicy" }
      ],
      "total_amount": 500.0,
      "status": "pending",
      "payment_method": "cash",
      "created_at": "2026-10-23T15:10:00Z"
    }
  }
  ```

#### Poll Order Status
```http
GET /orders/{id}/status
```
- **Response**:
  ```json
  {
    "ok": true,
    "order_id": "e6a1f33b-...",
    "status": "preparing",
    "table_number": 3,
    "items": [...],
    "total_amount": 500.0,
    "payment_method": "cash",
    "created_at": "2026-10-23T15:10:00Z"
  }
  ```

#### Mark Order as Billed (Cashier / POS)
```http
POST /orders/{id}/mark-billed
Content-Type: application/json

{ "payment_method": "card" }  // "cash" | "card" | "upi"
```

#### Submit Customer Feedback
```http
POST /orders/{id}/feedback
Content-Type: application/json

{
  "rating": 5,
  "comment": "The ramen was exceptional and the lucky cat waiter was super fun!"
}
```
- Rating must be an integer between 1 and 5.

---

### 3.6 Kitchen Display System (KDS) REST Endpoints

All KDS endpoints require the header `X-Admin-Key: <ADMIN_API_KEY>`.

#### 1. Fetch Active Orders for KDS Display
```http
GET /admin/orders/active?restaurant_id=e709b814-b895-4c2a-bec2-dad69019ccde
X-Admin-Key: <ADMIN_API_KEY>
```
- **Purpose**: Returns all orders currently in `pending`, `preparing`, or `ready` status.
- **Features**:
  - Orders are sorted **chronologically (earliest first)** for kitchen priority.
  - Automatically resolves `is_veg` boolean flags for every item by looking up the restaurant's menu catalog.
- **Response**:
  ```json
  {
    "orders": [
      {
        "id": "e6a1f33b-...",
        "table_number": 3,
        "status": "pending",
        "items": [
          {
            "name": "Miso Ramen",
            "qty": 2,
            "is_veg": false,
            "instructions": "Extra spicy"
          },
          {
            "name": "Edamame",
            "qty": 1,
            "is_veg": true,
            "instructions": ""
          }
        ],
        "created_at": "2026-10-23T15:05:00Z"
      }
    ]
  }
  ```

#### 2. Advance Order Status
```http
PATCH /orders/{order_id}/status
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{
  "status": "preparing"
}
```
- **Allowed Transitions**:
  - `pending` ➔ `preparing` | `cancelled`
  - `preparing` ➔ `ready` | `cancelled`
  - `ready` ➔ `delivered` | `cancelled`
  - `delivered` ➔ `billed`
- **Response**:
  ```json
  {
    "success": true,
    "order_id": "e6a1f33b-...",
    "status": "preparing",
    "updated_at": "2026-10-23T15:15:00Z",
    "order": { ... }
  }
  ```

---

### 3.7 Admin Dashboard & Analytics REST Endpoints

All Admin routes require `X-Admin-Key: <ADMIN_API_KEY>`.

#### 1. Sales & Revenue Analytics Summary
```http
GET /admin/analytics/sales?restaurant_id={uuid}&range=7d
X-Admin-Key: <ADMIN_API_KEY>
```
- **Query Parameters**:
  - `range`: `all` | `1d` | `7d` | `1m` | `custom` (default: `7d`)
  - `start_date`: `YYYY-MM-DD` (optional, for custom range)
  - `end_date`: `YYYY-MM-DD` (optional, for custom range)
- **Response**:
  ```json
  {
    "total_revenue": 14250.0,
    "total_orders": 45,
    "average_order_value": 356.25,
    "top_items": [
      { "name": "Miso Ramen", "sold": 38, "revenue": 9500.0 },
      { "name": "Veggie Gyoza", "sold": 22, "revenue": 3960.0 }
    ],
    "timeline": [
      { "date": "2026-10-20", "revenue": 4500.0, "orders": 14 },
      { "date": "2026-10-21", "revenue": 5200.0, "orders": 17 },
      { "date": "2026-10-22", "revenue": 4550.0, "orders": 14 }
    ]
  }
  ```

#### 2. Feedback & Ratings Analytics Summary
```http
GET /admin/analytics/feedback?restaurant_id={uuid}&range=7d
X-Admin-Key: <ADMIN_API_KEY>
```
- **Query Parameters**:
  - `range`: `all` | `7d` | `1m` (default: `7d`)
- **Response**:
  ```json
  {
    "average_rating": 4.8,
    "total_reviews": 25,
    "distribution": { "5": 20, "4": 4, "3": 1, "2": 0, "1": 0 },
    "recent_comments": [
      {
        "order_id": "...",
        "rating": 5,
        "comment": "Best ramen in town!",
        "created_at": "2026-10-23T14:30:00Z"
      }
    ]
  }
  ```

#### 3. Full Menu Catalog (Admin View)
```http
GET /admin/menu?restaurant_id={uuid}
X-Admin-Key: <ADMIN_API_KEY>
```
- **Response**: Returns complete menu including sold out (`is_available: false`) items.
  ```json
  {
    "items": [
      {
        "id": "c9b7405e-...",
        "name": "Edamame",
        "category": "Starters",
        "price": 120.0,
        "is_veg": true,
        "is_spicy": false,
        "is_available": false,
        "allergens": ["soy"]
      }
    ]
  }
  ```

#### 4. Toggle Menu Item Availability (Sold Out / In Stock)
```http
PATCH /admin/menu/{item_id}/availability
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{
  "is_available": true
}
```
- **Effect**: Toggles item availability directly by UUID and immediately busts the menu cache and system prompt cache.

#### 5. Update Menu Item Price
```http
PATCH /admin/menu/{item_id}/price
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{
  "price": 280.0
}
```
- **Effect**: Updates item price in database, validates price > 0, takes immediate effect on future order confirmations, and busts the menu cache.

---

## 4. MCP Tools Reference (Model Context Protocol)

The server exposes **19 registered MCP tools** across two isolated MCP sub-apps with Streamable HTTP transport:

### A. Customer MCP Tools (`/mcp/customer`) — 10 Tools

Bound to the active dining session. Called autonomously by the AI orchestrator.  
**Required Headers**:
- `X-Session-Id: <uuid>`
- `Authorization: Bearer <INTERNAL_MCP_TOKEN>`

| Tool Name | Parameters & Types | Description & Validations |
| :--- | :--- | :--- |
| `get_customer_context` | `{}` | Returns customer profile: name, dietary preferences, visit count, and top 5 favorite dishes. Returns guest profile if anonymous. |
| `search_menu` | `query?: str`, `category?: str`, `veg_only?: bool = False`, `spicy?: bool \| None = None`, `max_price?: float \| None = None`, `limit?: int = 10` | Searches available dishes using multi-attribute AND filters. Limit capped at 50. Uses menu cache. |
| `get_menu_item` | `name: str` | Resolves item by name: Exact ➔ Prefix ➔ Contains matching. Detects ambiguity and returns candidate list if multiple matches exist. |
| `get_current_order` | `{}` | Returns open draft order items, subtotals, item count, and total cart amount. |
| `add_item` | `name: str`, `qty?: int = 1`, `instructions?: str = ""` | Validates live menu availability and adds dish to draft cart. Accumulates quantity if already present. Capped to 200 char instructions. Executes `draft_apply` RPC. |
| `remove_item` | `name: str` | Removes an item completely from the open draft cart via `draft_apply` RPC. |
| `set_quantity` | `name: str`, `qty: int` | Sets absolute quantity in draft. `qty: 0` removes the item. Validates availability for positive quantities. |
| `clear_order` | `{}` | Clears all items from the current open draft order cart. |
| `get_order_status` | `{}` | Checks the kitchen preparation status of the latest non-billed active order for this session's table. |
| `recommend_dishes` | `veg_only?: bool = False`, `exclude_spicy?: bool = False`, `limit?: int = 6` | Ranks dishes using past customer orders (+2), preference matches (+1), and restaurant popularity. Each result contains a non-empty `reasons` list. |

---

### B. Admin MCP Tools (`/mcp/admin`) — 9 Tools

Protected administrative tools for kitchen staff and restaurant managers.  
**Authentication**: Header `X-Admin-Key: <ADMIN_API_KEY>` or parameter `admin_key: str`.

| Tool Name | Parameters & Types | Description & Validations |
| :--- | :--- | :--- |
| `list_orders` | `restaurant_id: str`, `status?: str`, `table_number?: int`, `active_only?: bool = False`, `limit?: int = 20`, `admin_key?: str` | Lists orders for a restaurant. `active_only=True` excludes billed and cancelled orders. Limit clamped between 1 and 100. |
| `get_order` | `order_id: str`, `admin_key?: str` | Retrieves full order details including line items, customer phone, table number, total, and timestamps. |
| `update_order_status` | `order_id: str`, `status: str`, `admin_key?: str` | Advances order lifecycle: `pending` ➔ `preparing` ➔ `ready` ➔ `delivered` ➔ `billed` (or `cancelled` from pending/preparing/ready). |
| `mark_billed` | `order_id: str`, `payment_method?: str = "cash"`, `admin_key?: str` | Records payment method (`cash`, `card`, `upi`) and marks order as `billed`. |
| `sales_summary` | `restaurant_id: str`, `days?: int = 7`, `admin_key?: str` | Aggregates revenue for billed orders, total orders count, top 10 dishes by sold quantity, status breakdown, and daily timeline. |
| `feedback_summary` | `restaurant_id: str`, `days?: int = 7`, `admin_key?: str` | Aggregates customer feedback ratings (1–5 breakdown), average rating score, and last 10 comment reviews. |
| `set_item_availability` | `restaurant_id: str`, `name: str`, `is_available: bool`, `admin_key?: str` | Toggles item stock status (`is_available = true/false`) by fuzzy name match and busts menu & prompt cache. |
| `update_item_price` | `restaurant_id: str`, `name: str`, `price: float`, `admin_key?: str` | Updates dish price (must be > 0), takes effect immediately on future confirmations, and busts menu & prompt cache. |
| `get_menu` | `restaurant_id: str`, `include_unavailable?: bool = False`, `admin_key?: str` | Retrieves complete restaurant menu catalog with allergen tags, pricing, and category metadata. |

---

## 5. Personas & Character Voice

The server implements **5 distinct waiter personas** configurable via the `character` field in `POST /sessions`:

| Persona ID | Character Name | Role & Personality | Tone, Greeting & Actions |
| :--- | :--- | :--- | :--- |
| `neko` *(Default)* | **Maneki Neko** | Cheerful Japanese lucky cat robot waiter. Warm, charming, and playful. | **Tone**: Warm, cheerful, polite, playful with subtle cat charm.<br>**Greeting**: *"Irasshaimase! Nya~ Welcome to our restaurant! How may I serve you today?"*<br>**Actions & Tags**: `*waves paw*`, `*purrs happily*`, `[happy]` |
| `butler` | **Sebastian** | Distinguished, formal, impeccably attentive butler. | **Tone**: Refined, dignified, courteous, polished, highly professional.<br>**Greeting**: *"A very pleasant welcome to you. It is my absolute privilege to attend to your table today."*<br>**Actions & Tags**: `*bows respectfully*`, `[polite]` |
| `chef` | **Chef Kenji** | Passionate culinary artisan and flavor connoisseur. | **Tone**: Enthusiastic, passionate about ingredients, knowledgeable, appetizing.<br>**Greeting**: *"Welcome to our kitchen's dining room! I can't wait to share today's freshest flavors with you!"*<br>**Actions & Tags**: `*adjusts apron*`, `*beams with pride*`, `[excited]` |
| `anime` | **Chibi Aoi** | High-energy, bubbly, upbeat anime mascot robot. | **Tone**: Energetic, bubbly, joyful, full of optimism and cheer.<br>**Greeting**: *"Yaho~! Welcome welcome! I am super thrilled to be your dining buddy today!"*<br>**Actions & Tags**: `*spins happily*`, `*eyes sparkle*`, `[excited]`, `[happy]` |
| `zen` | **Master Ren** | Serene, calm, mindful tea-house style attendant. | **Tone**: Peaceful, soothing, mindful, grounded, patient.<br>**Greeting**: *"Peace and welcome to this tranquil space. Please take your time and relax."*<br>**Actions & Tags**: `*bows gently*`, `*pours imaginary tea*`, `[calm]`, `[thoughtful]` |

### Critical Domain Rules (Enforced in System Prompt)

1. **No Invented Dishes or Prices**: The AI must never invent dishes or prices. If a compact Menu Snapshot is present in the prompt, it may answer from it; otherwise, it must look up items via `search_menu` or `get_menu_item`.
2. **Quantity Accuracy & Serving Size Invariance**: Suffixes like `(2pcs)` or `(4pcs)` represent portion sizes only and do not multiply order quantities (e.g. 6 Gulab Jamun (2pcs) = qty 6).
3. **Never Claim Order is Confirmed**: The AI informs customers that dishes are in their **draft order** and directs them to tap the on-screen "Confirm Order" button.
4. **Availability & Stockouts**: Informs the customer politely when an item is unavailable and suggests in-stock alternatives.
5. **Ambiguous Matches**: When multiple candidate dishes match, presents choices clearly to the customer.
6. **Emotion Tags and Action Asterisks**: Uses bracket emotion tags (`[happy]`, `[thinking]`, `[curious]`, `[excited]`, `[apologetic]`, `[calm]`) and action asterisks (`*bows*`, `*waves paw*`).
7. **Strictly No Unicode Emojis**: Never emits Unicode emojis (such as 🐱, 🍜, 🙏, 😊). The frontend parser relies exclusively on plain text, asterisks, and bracket tags for avatar animation.
8. **Natural Hinglish & Multilingual**: Fully understands and responds in conversational Hinglish (e.g. *"Ek spicy ramen laga do"*, *"3 aur le aao"*, *"Kuch meetha suggest karo"*).
9. **Pre-Tool Response Sentence**: Before calling any tool, emits one conversational sentence in voice and language so tokens stream immediately to the customer.
10. **Brevity**: Replies in at most 2 short sentences unless the customer asks for details. Never repeats the entire order back unprompted. Only states an item was added after the tool confirms success.
11. **Direct Speech Only**: Strictly suppresses internal monologue, reasoning traces, or thinking processes (`chat_template_kwargs: {"thinking": False}`).

---

## 6. Database Schema & Postgres RPC Architecture

### Relational Tables

| Table | Primary Key | Key Columns | Indexes / Notes |
| :--- | :--- | :--- | :--- |
| `restaurants` | `id (uuid)` | `name`, `created_at` | Tenant root |
| `menu_items` | `id (uuid)` | `restaurant_id`, `name`, `category`, `price`, `is_veg`, `is_spicy`, `is_available`, `allergens` | Indexed on `restaurant_id`, `is_available` |
| `customers` | `id (uuid)` | `restaurant_id`, `name`, `phone`, `visit_count`, `preferences` | Indexed on `(restaurant_id, phone)` |
| `sessions` | `id (uuid)` | `restaurant_id`, `table_number`, `customer_id`, `character`, `expires_at`, `created_at` | Indexed on `id`, `expires_at` |
| `order_drafts` | `id (uuid)` | `session_id`, `restaurant_id`, `items (jsonb)`, `status`, `order_id`, `created_at`, `updated_at` | Status: `'open'`, `'confirmed'`, `'cancelled'` |
| `orders` | `id (uuid)` | `restaurant_id`, `customer_id`, `table_number`, `items (jsonb)`, `total_amount`, `status`, `payment_method`, `created_at` | Status machine validated |
| `feedback` | `id (uuid)` | `order_id`, `rating (int 1-5)`, `comment`, `created_at` | Indexed on `order_id` |
| `conversations`| `id (uuid)` | `session_id`, `restaurant_id`, `created_at` | One conversation per session |
| `messages` | `id (uuid)` | `conversation_id`, `role`, `content`, `tool_name`, `tool_call_id`, `created_at` | Roles: `'user'`, `'assistant'`, `'tool'` |

### Stored Procedures (Postgres RPC)

#### 1. `draft_apply`
```sql
draft_apply(
  p_session_id    uuid,
  p_restaurant_id uuid,
  p_op            text,       -- 'add' | 'set' | 'remove' | 'clear'
  p_name          text,
  p_qty           integer,
  p_price         numeric,
  p_instructions  text
) RETURNS SETOF order_drafts
```
- Locks the open draft row using `SELECT ... FOR UPDATE` (or inserts one if none exists).
- Performs the specified operation on the `items` JSONB array:
  - `add`: Accumulates quantity if dish already present, otherwise appends.
  - `set`: Overwrites absolute quantity (deletes if `p_qty = 0`).
  - `remove`: Filters out matching dish name (case-insensitive).
  - `clear`: Empties the items array.
- Persists changes and returns the updated draft in a single atomic transaction.

#### 2. `confirm_draft`
```sql
confirm_draft(
  p_session_id uuid,
  p_items      jsonb,
  p_total      numeric
) RETURNS SETOF orders
```
- Atomically marks the open draft as `confirmed`.
- Creates a new record in `orders` with initial status `pending`.
- Links `order_id` back to the draft for strict idempotency.

---

## 7. Environment Variables Reference

| Variable | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `SUPABASE_URL` | String | Supabase project URL | *(Required)* |
| `SUPABASE_SERVICE_KEY` | String | Supabase service-role key (server-only) | *(Required)* |
| `ADMIN_API_KEY` | String | Secret key for `/mcp/admin` and admin REST routes | *(Required)* |
| `SESSION_TOKEN_SECRET` | String | Dedicated HMAC secret for signing session tokens | `""` *(falls back to `ADMIN_API_KEY`)* |
| `INTERNAL_MCP_TOKEN` | String | Bearer token for Orchestrator ➔ `/mcp/customer` | *(Required)* |
| `NVIDIA_API_KEY` | String | API key for NVIDIA NIM inference | `""` |
| `NVIDIA_MODEL` | String | NIM model identifier | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `NVIDIA_ENDPOINT` | String | NIM chat completions endpoint | `https://integrate.api.nvidia.com/v1/chat/completions` |
| `NIM_STREAM` | Boolean | Enable true SSE streaming from NVIDIA NIM | `true` |
| `NIM_MAX_TOKENS` | Integer | Maximum tokens generated per assistant turn | `200` |
| `TOOL_TIMEOUT_S` | Float | Per-tool execution timeout in seconds | `5.0` |
| `DEBUG_TIMING` | Boolean | Include timing metrics object in SSE `event: done` | `false` |
| `HISTORY_MESSAGES` | Integer | Number of previous conversation turns in context | `20` |
| `MENU_CACHE_TTL` | Integer | Menu catalog TTL in seconds | `45` |
| `SESSION_CACHE_TTL` | Integer | Session validity TTL in seconds | `30` |
| `PROFILE_CACHE_TTL` | Integer | Customer profile cache TTL in seconds | `120` |
| `POPULARITY_CACHE_TTL` | Integer | Popularity scores cache TTL in seconds | `300` |
| `MENU_IN_PROMPT_MAX` | Integer | Max available dishes for inline prompt snapshot | `60` |
| `MENU_TABLE` | String | Table name for menu catalog | `menu_items` |
| `ORDERS_TABLE` | String | Table name for orders | `orders` |
| `CUSTOMERS_TABLE` | String | Table name for customer profiles | `customers` |
| `FEEDBACK_TABLE` | String | Table name for customer feedback | `feedback` |
| `CORS_ORIGINS` | String | Comma-separated allowed CORS origins | `http://localhost:3000` |
| `SESSION_TTL_HOURS` | Integer | Session lifespan in hours | `6` |
| `LOG_LEVEL` | String | Application logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `PORT` | Integer | Web server listening port | `8000` |
