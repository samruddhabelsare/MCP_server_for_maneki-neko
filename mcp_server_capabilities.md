# Maneki Neko MCP Server — Full Capabilities Reference

**Live URL:** `https://mcp-server-for-maneki-neko.onrender.com`  
**Docs UI:** `https://mcp-server-for-maneki-neko.onrender.com/docs`

---

## Architecture Overview

The server exposes **two layers**:

| Layer | Purpose | Used by |
|---|---|---|
| **REST API** | Session lifecycle, order panel UI, streaming chat | Your frontend (`app.js`) |
| **MCP Tools** | AI waiter's internal tool-calling (LLM does this) | NVIDIA NIM / orchestrator |

The AI waiter runs entirely server-side — your frontend only calls REST endpoints.

```
Frontend → POST /sessions/{id}/chat  →  Orchestrator (NVIDIA NIM)
                                              ↓  tool calls
                                         MCP Customer Tools (Supabase)
                                              ↓  SSE stream back
Frontend ← token / draft / done events
```

---

## REST Endpoints (Frontend calls these)

### 🔐 Auth Pattern
All session endpoints accept:  
`Authorization: Bearer <session_id>` (optional, for extra security)

---

### 1. Health Check
```
GET /healthz
```
Returns `{ status, uptime_seconds, supabase, version }`.  
Returns `503` if Supabase is unreachable.

---

### 2. Session Management

#### Create Session
```
POST /sessions
Content-Type: application/json

{
  "restaurant_id": "<uuid>",
  "table_number": 3,
  "phone": "+911234567890",   // optional — links customer profile
  "name": "Riya",             // optional — for new customers
  "preferences": ["veg"],     // optional
  "character": "neko",        // AI persona: "neko" | "sakura" | "hiro" | "yuki"
  "guest": false              // true = anonymous session, no profile
}
```
Returns `session_id` — **store this** for all subsequent calls.

#### Get Conversation History (for page refresh / tab restore)
```
GET /sessions/{id}/messages
Authorization: Bearer <session_id>
```
Returns last 50 chat messages in chronological order.

#### Get Order History (customer's past visits)
```
GET /sessions/{id}/history
Authorization: Bearer <session_id>
```
Returns last 10 confirmed orders for this customer.

---

### 3. Chat (AI Waiter — SSE Streaming)
```
POST /sessions/{id}/chat
Authorization: Bearer <session_id>
Content-Type: application/json

{ "message": "I'd like to order 2 Miso Soups please" }
```

**Returns:** `text/event-stream` with 3 event types:

| Event | Payload | Meaning |
|---|---|---|
| `token` | `{ "token": "Sure!" }` | Streaming text chunk from AI |
| `draft` | `{ "draft": { items, total } }` | Cart was updated by AI tool call |
| `done` | `{ "finish_reason": "stop" }` | Stream complete |

**Example JS consumer:**
```js
const es = new EventSource(/* can't POST with EventSource */);
// Use fetch + ReadableStream instead:
const res = await fetch(`/sessions/${sessionId}/chat`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${sessionId}` },
  body: JSON.stringify({ message: userInput })
});
const reader = res.body.getReader();
// read chunks, parse "data: {...}\n\n" lines
```

---

### 4. Order Draft Panel (UI-driven, no AI needed)

#### View draft
```
GET /sessions/{id}/draft
```

#### Update item quantity (from +/- buttons)
```
PATCH /sessions/{id}/draft/items/{item_name}
{ "qty": 2 }   // qty=0 removes the item
```

#### Delete item (✕ button)
```
DELETE /sessions/{id}/draft/items/{item_name}
```

---

### 5. Order Placement

#### Confirm Order (customer presses "Place Order")
```
POST /sessions/{id}/confirm
Authorization: Bearer <session_id>
```
- Atomically converts draft → order via Postgres RPC  
- Recomputes total from live DB prices  
- **Idempotent** — safe to call twice  
Returns `{ order: { id, status, items, total_amount } }`

#### Poll Order Status (kitchen tracker)
```
GET /orders/{id}/status
```
Status flow: `pending → preparing → ready → delivered → billed`

#### Mark as Billed (payment collected)
```
POST /orders/{id}/mark-billed
{ "payment_method": "cash" }   // "cash" | "card" | "upi"
```

---

### 6. Feedback
```
POST /orders/{id}/feedback
{ "rating": 4, "comment": "Great food!" }
```
Rating must be 1–5.

---

## MCP Tools (used internally by the AI)

Your frontend does **not** call these directly. The AI orchestrator calls them during tool-calling loops. Documented here so you understand what the AI can and cannot do.

### Customer MCP (`/mcp/customer`) — 10 tools

| Tool | What it does |
|---|---|
| `get_customer_context` | Loads customer name, visit count, top 5 favorites |
| `search_menu` | Filter menu by text query, category, veg-only, spicy, max price |
| `get_menu_item` | Look up exact item by name (fuzzy: exact → prefix → contains) |
| `get_current_order` | View current draft with subtotals |
| `add_item` | Add item + qty + special instructions to draft |
| `remove_item` | Remove an item completely from draft |
| `set_quantity` | Set exact qty for an item (0 = remove) |
| `clear_order` | Wipe entire draft |
| `get_order_status` | Check if the placed order is pending/preparing/ready |
| `recommend_dishes` | Personalized ranked suggestions (uses history + popularity) |

### Admin MCP (`/mcp/admin`) — 9 tools (protected by `X-Admin-Key`)

| Tool | What it does |
|---|---|
| `list_orders` | Filter orders by status / table / active-only |
| `get_order` | Get full order details by ID |
| `update_order_status` | Advance order: pending→preparing→ready→delivered |
| `mark_billed` | Record payment and close order |
| `sales_summary` | Revenue, top items, order counts (last N days) |
| `feedback_summary` | Average rating, breakdown, recent comments (last N days) |
| `set_item_availability` | Mark item as sold out / back in stock |
| `update_item_price` | Change menu item price (takes effect immediately) |
| `get_menu` | Full menu dump (can include unavailable items) |

---

## AI Personas

Pass `character` in `POST /sessions`:

| Value | Persona |
|---|---|
| `neko` | Cheerful kawaii cat (default) |
| `sakura` | Elegant, poetic |
| `hiro` | Confident, direct |
| `yuki` | Calm, minimalist |

---

## Key Constraints & Behaviours

- **AI never places the order** — it only manages the draft. The customer must press the Confirm button (calls `POST /sessions/{id}/confirm`).
- **Draft is live** — any `draft` SSE event means the cart changed; re-render the order panel immediately.
- **Session expires** — sessions have a TTL. On expiry, create a new one.
- **Fuzzy item matching** — the AI uses `exact → prefix → contains` matching. Typos are handled.
- **Idempotent confirm** — safe to call multiple times, won't double-order.
- **Recommendation reasons** — the AI explains *why* it suggested a dish; don't invent extra reasons in UI copy.

---

## Environment Variables Required

```
SUPABASE_URL=
SUPABASE_KEY=
NVIDIA_API_KEY=
INTERNAL_MCP_TOKEN=    # Bearer token for /mcp/customer
ADMIN_API_KEY=         # X-Admin-Key header for /mcp/admin
CORS_ORIGINS=          # comma-separated allowed origins
```
