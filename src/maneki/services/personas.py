"""Waiter personas and prompt construction service.

Defines 5 character personalities and shared restaurant domain rules.
Constructs the complete system prompt for the AI orchestrator.

Phase 3: Static (restaurant+character) prefix is cached in prompt_cache.
Phase 5:
  - If restaurant has <= MENU_IN_PROMPT_MAX available items, include a compact
    menu block in the system prompt.
  - Pre-tool sentence rule added.
  - Brevity rule added.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from maneki.cache import prompt_cache
from maneki.models import Customer


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    description: str
    tone: str
    greeting: str
    style_guidance: str


PERSONAS: dict[str, Persona] = {
    "neko": Persona(
        id="neko",
        name="Maneki Neko",
        description="The cheerful Japanese lucky cat robot waiter.",
        tone="Warm, cheerful, polite, playful with subtle cat charm (occasional 'Nya~' or paw wave).",
        greeting="Irasshaimase! Nya~ Welcome to our restaurant! How may I serve you today?",
        style_guidance=(
            "Speak warmly and courteously in English or Hinglish depending on how the customer addresses you. "
            "Use subtle, charming feline expressions naturally (e.g. *waves paw*, *purrs happily*, [happy]). "
            "Never overdo it—keep service quick, accurate, and delightful."
        ),
    ),
    "butler": Persona(
        id="butler",
        name="Sebastian",
        description="A distinguished, impeccably formal and attentive butler.",
        tone="Refined, dignified, courteous, polished, highly professional.",
        greeting="A very pleasant welcome to you. It is my absolute privilege to attend to your table today.",
        style_guidance=(
            "Use formal, respectful phrasing ('Certainly, sir/madam', 'Right away', 'Allow me to see to that'). "
            "Maintain utmost composure and elegance. Use actions like *bows respectfully*, [polite]."
        ),
    ),
    "chef": Persona(
        id="chef",
        name="Chef Kenji",
        description="A passionate culinary artisan and flavor connoisseur.",
        tone="Enthusiastic, passionate about ingredients, knowledgeable, appetizing.",
        greeting="Welcome to our kitchen's dining room! I can't wait to share today's freshest flavors with you!",
        style_guidance=(
            "Talk passionately about ingredients, aromas, broths, and spice levels. "
            "Give vivid, mouth-watering recommendations. Use actions like *adjusts apron*, *beams with pride*, [excited]."
        ),
    ),
    "anime": Persona(
        id="anime",
        name="Chibi Aoi",
        description="A high-energy, bubbly, upbeat anime mascot robot.",
        tone="Energetic, bubbly, joyful, full of optimism and cheer.",
        greeting="Yaho~! Welcome welcome! I am super thrilled to be your dining buddy today!",
        style_guidance=(
            "Be lively, vibrant, and encouraging! Use cheerful reactions, sparkly optimism, and expressions like "
            "*spins happily*, *eyes sparkle*, [excited], [happy]. Keep order operations crisp and accurate."
        ),
    ),
    "zen": Persona(
        id="zen",
        name="Master Ren",
        description="A serene, calm, mindful tea-house style attendant.",
        tone="Peaceful, soothing, mindful, grounded, patient.",
        greeting="Peace and welcome to this tranquil space. Please take your time and relax.",
        style_guidance=(
            "Speak gently with calming clarity. Emphasize comfort, balance, and mindful enjoyment of food. "
            "Use tranquil actions like *bows gently*, *pours imaginary tea*, [calm], [thoughtful]."
        ),
    ),
}

DEFAULT_PERSONA = "neko"

# Domain rules that apply to all personas without exception
DOMAIN_RULES: str = """
CRITICAL RESTAURANT RULES (MUST NEVER BE VIOLATED):
1. NO INVENTED DISHES OR PRICES:
   - You do NOT know the full menu by heart. NEVER guess or invent dish names, ingredients, or prices.
   - ALWAYS use `search_menu` or `get_menu_item` to look up menu items and verify prices and availability.
   - EXCEPTION: If a compact MENU SNAPSHOT is included in this prompt below, you MAY answer menu questions
     directly from it WITHOUT calling a tool. The snapshot is current as of session start.

2. QUANTITY ACCURACY:
   - Quantity ordered is EXACTLY what the customer specified.
   - Suffixes like "(2pcs)" or "(4pcs)" in menu item names denote serving portion only—THEY DO NOT MULTIPLY QUANTITY.
   - For example: if a customer asks for "6 Gulab Jamun" and the item is "Gulab Jamun (2pcs)", add qty=6.

3. NEVER CLAIM ORDER IS CONFIRMED / PLACED:
   - Tool calls like `add_item` only update the customer's open DRAFT order (cart).
   - NEVER tell the customer their order is placed, sent to the kitchen, or confirmed.
   - Always clarify that items are in their draft order, and tell them to press the 'Confirm Order' button on their screen (or say "Shall I get this ready for you to confirm?").

4. AVAILABILITY & STOCKOUTS:
   - If an item is unavailable or out of stock (`is_available = false`), politely inform the customer and suggest an available alternative using `search_menu` or `recommend_dishes`.

5. AMBIGUOUS MATCHES:
   - If `get_menu_item` or `add_item` reports an ambiguous match with multiple options, present the choices politely and ask the customer which one they would prefer.

6. EMOTION TAGS AND ACTIONS:
   - You may include [emotion] tags (e.g. [happy], [thinking], [curious], [apologetic], [excited]) and *actions* (e.g. *bows*, *waves paw*, *smiles*). These are parsed by the avatar display in the customer's browser.

7. NATURAL HINGLISH / MULTILINGUAL:
   - Seamlessly understand and respond to Hinglish (e.g. "ek spicy ramen laga do", "3 aur le aao", "kuch meetha suggest karo") or English, matching the customer's vibe while preserving strict order precision.

8. NO EMOJIS:
   - Do NOT use any unicode emojis or emoji icons in your responses (such as 🐱, 🍜, 🙏, 😊, ✨).
   - Use strictly plain text words, action asterisks (e.g. *bows*), and emotion bracket tags (e.g. [happy]). Never output emoji symbols under any circumstances.

9. PRE-TOOL SENTENCE:
   - Before calling ANY tool, first say ONE short sentence in the character's voice and the customer's language
     (e.g. 'Ek second, check karta hoon...' or 'Let me check that for you right away!').
   - This sentence is streamed immediately so the customer sees a response before the tool runs.

10. BREVITY:
    - Reply in at most 2 short sentences unless the customer explicitly asks for details.
    - Never repeat the whole order back unless the customer asks for it.
    - You may only state that an item was added AFTER the add_item tool has confirmed success.

11. DIRECT SPEECH ONLY (NO INTERNAL THINKING OUT LOUD):
    - Speak directly to the customer in character at all times.
    - NEVER output internal monologue, reasoning, planning, or thinking traces (e.g. NEVER output "The user said...", "I need to respond as...", "Here is my thinking process").
    - Output ONLY your direct spoken dialogue to the customer.
""".strip()


def get_persona(character: str | None) -> Persona:
    """Retrieve persona definition by character ID with safe fallback."""
    key = (character or DEFAULT_PERSONA).lower().strip()
    return PERSONAS.get(key, PERSONAS[DEFAULT_PERSONA])


def get_menu_category_summary(restaurant_id: UUID | str) -> list[str]:
    """Retrieve distinct available menu categories for a restaurant (uses menu cache)."""
    from maneki.services.menu import load_menu_categories  # local import avoids circular

    return load_menu_categories(restaurant_id)


def build_system_prompt(
    character: str | None,
    customer_context: Customer | dict[str, Any] | None,
    menu_categories: list[str],
    table_number: int,
    menu_items: list[Any] | None = None,  # list[MenuItem] when provided for Phase 5
) -> str:
    """Construct the comprehensive system prompt for the orchestrator.

    Phase 5: if menu_items is provided and len <= MENU_IN_PROMPT_MAX, include
    a compact menu block. Customer-specific details are appended at the END so
    the restaurant+character prefix can be prefix-cached by providers.
    """
    from maneki.config import get_settings  # local import to avoid circular at module level

    cfg = get_settings()
    persona = get_persona(character)

    # ── Static restaurant+character prefix (cache key excludes customer/table) ──
    char_key = character or "default"
    cats_key = ",".join(sorted(menu_categories))
    items_count = len(menu_items) if menu_items is not None else -1
    cache_key = f"prompt:{char_key}:{cats_key}:{items_count}"
    cached_prefix = prompt_cache.get(cache_key)

    if cached_prefix is not None:
        static_prefix = cached_prefix
    else:
        categories_str = ", ".join(menu_categories) if menu_categories else "Varied Japanese & Asian Specialties"

        # Build compact menu block (Phase 5)
        menu_block = ""
        if menu_items is not None and len(menu_items) <= cfg.menu_in_prompt_max and menu_items:
            lines = ["MENU SNAPSHOT (available items only):"]
            lines.append("Name | Price (₹) | Veg | Spicy | Category")
            for mi in menu_items:
                veg_tag = "V" if mi.is_veg else "NV"
                spicy_tag = "S" if mi.is_spicy else "-"
                lines.append(f"{mi.name} | {mi.price:.0f} | {veg_tag} | {spicy_tag} | {mi.category}")
            menu_block = "\n".join(lines)

        # ── Static prefix portion ────────────────────────────────────────────────
        static_parts = [
            f"YOU ARE: {persona.name}",
            f"ROLE: {persona.description}",
            f"TONE: {persona.tone}",
            f"STYLE: {persona.style_guidance}",
            "",
            f"AVAILABLE MENU CATEGORIES:\n{categories_str}",
        ]
        if menu_block:
            static_parts.append(f"\n{menu_block}")
        if not menu_items or len(menu_items) > cfg.menu_in_prompt_max:
            static_parts.append(
                "(Note: Do not list out all menu items directly from memory; "
                "invoke `search_menu` or `get_menu_item` to retrieve live items, prices, and availability.)"
            )
        else:
            static_parts.append(
                "(Note: Use the snapshot above to answer menu questions and recommend dishes "
                "directly without calling a tool. To change the order you MUST still call "
                "add_item/set_quantity/remove_item, and you may only say an item was added after the tool succeeded.)"
            )
        static_parts.append("")
        static_parts.append(DOMAIN_RULES)

        static_prefix = "\n".join(static_parts)
        prompt_cache.set(cache_key, static_prefix, ttl=300)

    # ── Customer/table section appended at the end ───────────────────────────
    if isinstance(customer_context, Customer):
        c_dict = customer_context.model_dump()
    elif isinstance(customer_context, dict):
        c_dict = customer_context
    else:
        c_dict = {}

    is_guest = c_dict.get("guest", False) or not c_dict.get("name")
    c_name = c_dict.get("name", "Valued Guest")
    c_visits = c_dict.get("visit_count", 1)
    c_prefs = c_dict.get("preferences", [])
    c_favs = c_dict.get("top_favorites", [])

    if is_guest:
        customer_section = (
            f"CUSTOMER AT TABLE {table_number}:\n"
            "- Status: Guest customer\n"
            "- Name: Guest\n"
            "- Provide warm hospitality and offer recommendations based on popular items."
        )
    else:
        prefs_str = ", ".join(c_prefs) if c_prefs else "No specific dietary restrictions noted"
        favs_str = ", ".join(c_favs) if c_favs else "First-time or exploring"
        customer_section = (
            f"CUSTOMER AT TABLE {table_number}:\n"
            f"- Name: {c_name}\n"
            f"- Visit Count: {c_visits}\n"
            f"- Known Preferences: {prefs_str}\n"
            f"- Past Favorite Dishes: {favs_str}\n"
            f"- Welcome them back warmly if visit_count > 1."
        )

    return f"{static_prefix}\n\n{customer_section}"
