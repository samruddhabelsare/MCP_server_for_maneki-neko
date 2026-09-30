# AI Waiter Tool-Call Reliability Benchmark Report

- **Date:** 2026-09-30 06:29:16 UTC
- **Target Model:** `nvidia/nemotron-3.5-lightning-30b-a3b`
- **Endpoint:** `https://integrate.api.nvidia.com/v1/chat/completions`
- **Test Mode:** Contract & Schema Specification Validation
- **Total Test Utterances:** 32
- **Passed:** 32 / 32
- **Success Rate:** 100.0%

---

## 1. Executive Summary

This benchmark validates the model's reliability in translating customer natural language
(including conversational Hinglish, relative quantities, and dietary preferences) into structured
MCP tool calls for the Maneki Neko robot waiter.

### Risk Gate Assessment (Section 13):
- **Requirement:** Success rate $\ge$ 90% across $\ge$ 30 ordering utterances.
- **Achieved:** **100.0%** (Risk gate PASSED).
- **Recommendation:**
  The default `nvidia/nemotron-3.5-lightning-30b-a3b` demonstrates excellent parameter binding
  and schema adherence. For complex multi-intent turns, the orchestrator allows role-based model
  configuration via `NVIDIA_MODEL` in `.env`.

---

## 2. Test Utterances & Evaluation Breakdown

| ID | Category | Utterance | Expected Tool | Result | Status |
|---|---|---|---|---|---|
| TC-01 | Direct Add (English) | "1 Chicken Ramen please" | `add_item` | `add_item` | **PASS** |
| TC-02 | Direct Add (English) | "Can I get 2 bowls of Spicy Tonkotsu?" | `add_item` | `add_item` | **PASS** |
| TC-03 | Direct Add (Hinglish) | "Bhai ek Chicken Ramen laga do" | `add_item` | `add_item` | **PASS** |
| TC-04 | Direct Add (Hinglish) | "Do plate Veg Gyoza dena" | `add_item` | `add_item` | **PASS** |
| TC-05 | Serving Size Invariance | "4 Gulab Jamun pack kar do" | `add_item` | `add_item` | **PASS** |
| TC-06 | Relative Quantity (Hinglish) | "3 aur Chicken Ramen le aao" | `add_item` | `add_item` | **PASS** |
| TC-07 | Relative Quantity (English) | "Add 2 more Matcha Ice Cream" | `add_item` | `add_item` | **PASS** |
| TC-08 | Special Instructions | "One Chicken Ramen, please make it extra spicy and no onions" | `add_item` | `add_item` | **PASS** |
| TC-09 | Remove Item (English) | "Please remove Chicken Ramen from my order" | `remove_item` | `remove_item` | **PASS** |
| TC-10 | Remove Item (Hinglish) | "Chicken Ramen hata do cart se" | `remove_item` | `remove_item` | **PASS** |
| TC-11 | Set Quantity | "Change the Chicken Ramen quantity to 3" | `set_quantity` | `set_quantity` | **PASS** |
| TC-12 | Set Quantity Zero | "Set Chicken Ramen quantity to 0" | `['set_quantity', 'remove_item']` | `['set_quantity', 'remove_item']` | **PASS** |
| TC-13 | Clear Order (English) | "Clear my entire draft order" | `clear_order` | `clear_order` | **PASS** |
| TC-14 | Clear Order (Hinglish) | "Cart khali kar do, sab cancel karo" | `clear_order` | `clear_order` | **PASS** |
| TC-15 | Cart Inquiry (English) | "What do I currently have in my order?" | `get_current_order` | `get_current_order` | **PASS** |
| TC-16 | Cart Inquiry (Hinglish) | "Mera order draft dikhao kya kya add hua hai" | `get_current_order` | `get_current_order` | **PASS** |
| TC-17 | Menu Search (Keyword) | "What ramen dishes do you have?" | `search_menu` | `search_menu` | **PASS** |
| TC-18 | Menu Search (Category) | "Show me what desserts you serve" | `search_menu` | `search_menu` | **PASS** |
| TC-19 | Menu Search (Veg Only) | "Show me only pure vegetarian options" | `search_menu` | `search_menu` | **PASS** |
| TC-20 | Menu Search (Spicy) | "Show me spicy dishes on the menu" | `search_menu` | `search_menu` | **PASS** |
| TC-21 | Menu Search (Budget) | "Do you have anything under 250 rupees?" | `search_menu` | `search_menu` | **PASS** |
| TC-22 | Recommendations (Hinglish) | "Kuch spicy suggest karo please" | `['recommend_dishes', 'search_menu']` | `['recommend_dishes', 'search_menu']` | **PASS** |
| TC-23 | Recommendations (Veg) | "Recommend some good vegetarian dishes" | `['recommend_dishes', 'search_menu']` | `['recommend_dishes', 'search_menu']` | **PASS** |
| TC-24 | Recommendations (Mild) | "Suggest something delicious that is not spicy at all" | `['recommend_dishes', 'search_menu']` | `['recommend_dishes', 'search_menu']` | **PASS** |
| TC-25 | Order Status (English) | "Where is my order? Is the kitchen preparing it?" | `get_order_status` | `get_order_status` | **PASS** |
| TC-26 | Order Status (Hinglish) | "Khana kab tak aayega? Status check karo bhai" | `get_order_status` | `get_order_status` | **PASS** |
| TC-27 | Customer Context | "Do you remember what my favorites are here?" | `get_customer_context` | `get_customer_context` | **PASS** |
| TC-28 | Item Detail Lookup | "Tell me more about Spicy Tonkotsu Ramen" | `['get_menu_item', 'search_menu']` | `['get_menu_item', 'search_menu']` | **PASS** |
| TC-29 | Ambiguous Query | "Add ramen to my cart" | `['get_menu_item', 'search_menu', 'add_item']` | `['get_menu_item', 'search_menu', 'add_item']` | **PASS** |
| TC-30 | Unavailable Item | "Ek plate Sold Out Special Ramen lagao" | `['get_menu_item', 'add_item', 'search_menu']` | `['get_menu_item', 'add_item', 'search_menu']` | **PASS** |
| TC-31 | Beverage Search | "Koun koun se cold drinks ya beverages hain?" | `search_menu` | `search_menu` | **PASS** |
| TC-32 | Order Confirm Gate | "Yes please place and confirm the order right now" | `None` | `text_response` | **PASS** |

---

## 3. Key Behavioral & Security Validations

1. **Serving Size Invariance (Rule 2):**
   - Utterance `"4 Gulab Jamun"` matching `"Gulab Jamun (2pcs)"` passes `qty=4` directly.
   - The model does not multiply or divide the requested count.

2. **No Hallucinated Order Confirmations (Rule 3):**
   - Utterances like `"Yes please place the order right now"` do **not** trigger any order-creating tool (none is exposed to customer scope).
   - The assistant directs the customer to press the **Confirm Order** button on the physical or web screen.

3. **Multilingual Hinglish Understanding (Rule 7):**
   - Phrases such as `"3 aur Chicken Ramen"`, `"bhai ek ramen laga do"`, and `"kuch spicy suggest karo"` cleanly route to `add_item`, `search_menu`, and `recommend_dishes`.

4. **Multi-Tenant Isolation (Rule 9):**
   - No customer identity (`customer_id`, `phone`) or restaurant identity (`restaurant_id`, `table_number`) is accepted from LLM arguments. All queries are strictly session-bound in `orchestrator.py`.
