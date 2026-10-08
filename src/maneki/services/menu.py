"""Menu catalog and resolution service.

Implements exact -> prefix -> contains matching (with ambiguity detection),
search filtering (veg, spicy, category, max_price), and menu administration.

Phase 3 adds TTL-based caching for menu items and categories.
Cache is invalidated on every write (set_item_availability, update_item_price).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from maneki.cache import menu_cache
from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import AmbiguousItemError, ItemNotFoundError, ValidationError
from maneki.models import MenuItem


def load_menu(restaurant_id: UUID | str, include_unavailable: bool = False) -> list[MenuItem]:
    """Load all menu items for a restaurant (cached when include_unavailable=False)."""
    rid = str(restaurant_id)
    cfg = get_settings()

    # Only cache the available-items view (the hot path)
    if not include_unavailable:
        cache_key = f"menu:{rid}"
        cached = menu_cache.get(cache_key)
        if cached is not None:
            return cast(list[MenuItem], cached)

        db = get_db()
        res = (
            db.table(cfg.menu_table)
            .select("*")
            .eq("restaurant_id", rid)
            .eq("is_available", True)
            .execute()
        )
        rows = cast(list[dict[str, Any]], res.data) if res.data else []
        items = [_parse_menu_item(r) for r in rows]
        menu_cache.set(cache_key, items, ttl=cfg.menu_cache_ttl)
        return items

    # include_unavailable=True — bypass cache (admin path, rarely called)
    db = get_db()
    res = (
        db.table(cfg.menu_table)
        .select("*")
        .eq("restaurant_id", rid)
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    return [_parse_menu_item(r) for r in rows]


def load_menu_categories(restaurant_id: UUID | str) -> list[str]:
    """Return sorted distinct available category names (cached)."""
    rid = str(restaurant_id)
    cfg = get_settings()
    cache_key = f"cats:{rid}"
    cached = menu_cache.get(cache_key)
    if cached is not None:
        return cast(list[str], cached)

    items = load_menu(rid, include_unavailable=False)
    cats = sorted({item.category for item in items if item.category})
    menu_cache.set(cache_key, cats, ttl=cfg.menu_cache_ttl)
    return cats


def search_menu(
    restaurant_id: UUID | str,
    query: str | None = None,
    category: str | None = None,
    veg_only: bool = False,
    spicy: bool | None = None,
    max_price: float | None = None,
    limit: int = 10,
) -> list[MenuItem]:
    """Search available menu items with multiple combined filters."""
    items = load_menu(restaurant_id, include_unavailable=False)

    filtered: list[MenuItem] = []
    q = query.strip().lower() if query else None
    cat = category.strip().lower() if category else None

    for item in items:
        if veg_only and not item.is_veg:
            continue
        if spicy is not None and item.is_spicy != spicy:
            continue
        if max_price is not None and item.price > max_price:
            continue
        if cat and item.category.strip().lower() != cat:
            continue
        if q:
            name_match = q in item.name.lower()
            cat_match = q in item.category.lower()
            if not (name_match or cat_match):
                continue

        filtered.append(item)
        if len(filtered) >= limit:
            break

    return filtered


def get_menu_item(restaurant_id: UUID | str, name: str) -> MenuItem:
    """Resolve a menu item by name using: exact -> prefix -> contains.

    Always validates against LIVE menu (include_unavailable=True) so
    add_item/set_quantity can detect truly unavailable items.

    Raises:
        ItemNotFoundError: If no candidate matches.
        AmbiguousItemError: If prefix or contains matching yields multiple candidates.
    """
    target = name.strip().lower()
    if not target:
        raise ItemNotFoundError(name)

    all_items = load_menu(restaurant_id, include_unavailable=True)

    # 1. Exact match (case-insensitive, trimmed)
    exact_matches = [i for i in all_items if i.name.strip().lower() == target]
    if len(exact_matches) == 1:
        return exact_matches[0]

    # 2. Prefix match
    prefix_matches = [i for i in all_items if i.name.strip().lower().startswith(target)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise AmbiguousItemError(name, [i.name for i in prefix_matches])

    # 3. Contains match
    contains_matches = [i for i in all_items if target in i.name.strip().lower()]
    if len(contains_matches) == 1:
        return contains_matches[0]
    if len(contains_matches) > 1:
        raise AmbiguousItemError(name, [i.name for i in contains_matches])

    raise ItemNotFoundError(name)


def _invalidate_menu_cache(restaurant_id: UUID | str) -> None:
    """Invalidate all menu/category cache entries for a restaurant."""
    rid = str(restaurant_id)
    menu_cache.invalidate(f"menu:{rid}")
    menu_cache.invalidate(f"cats:{rid}")
    # Also bust prompt cache for this restaurant (Phase 5)
    from maneki.cache import prompt_cache  # local import to avoid circular
    prompt_cache.invalidate_prefix(f"prompt:")  # noqa: F541 — clear all prompts on menu change


def set_item_availability(
    restaurant_id: UUID | str,
    name: str,
    is_available: bool,
) -> MenuItem:
    """Set availability for a menu item."""
    item = get_menu_item(restaurant_id, name)
    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.menu_table)
        .update({"is_available": is_available})
        .eq("id", str(item.id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    raw = rows[0] if rows else {**item.model_dump(), "is_available": is_available}
    _invalidate_menu_cache(restaurant_id)
    return _parse_menu_item(raw)


def update_item_price(
    restaurant_id: UUID | str,
    name: str,
    price: float,
) -> MenuItem:
    """Update price for a menu item."""
    item = get_menu_item(restaurant_id, name)
    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.menu_table)
        .update({"price": price})
        .eq("id", str(item.id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    raw = rows[0] if rows else {**item.model_dump(), "price": price}
    _invalidate_menu_cache(restaurant_id)
    return _parse_menu_item(raw)


def set_item_availability_by_id(
    item_id: UUID | str,
    is_available: bool,
) -> MenuItem:
    """Set availability for a menu item directly by its UUID."""
    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.menu_table)
        .update({"is_available": is_available})
        .eq("id", str(item_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        raise ItemNotFoundError(str(item_id))
    result = _parse_menu_item(rows[0])
    _invalidate_menu_cache(result.restaurant_id)
    return result


def update_item_price_by_id(
    item_id: UUID | str,
    price: float,
) -> MenuItem:
    """Update price for a menu item directly by its UUID."""
    if price <= 0:
        raise ValidationError("Price must be greater than 0.")
    cfg = get_settings()
    db = get_db()
    res = (
        db.table(cfg.menu_table)
        .update({"price": price})
        .eq("id", str(item_id))
        .execute()
    )
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    if not rows:
        raise ItemNotFoundError(str(item_id))
    result = _parse_menu_item(rows[0])
    _invalidate_menu_cache(result.restaurant_id)
    return result


def _parse_menu_item(raw: dict[str, Any]) -> MenuItem:
    allergens = raw.get("allergens") or []
    if isinstance(allergens, str):
        allergens = [a.strip() for a in allergens.strip("{}").split(",") if a.strip()]

    return MenuItem(
        id=UUID(str(raw["id"])),
        restaurant_id=UUID(str(raw["restaurant_id"])),
        name=str(raw["name"]),
        category=str(raw["category"]),
        price=float(raw["price"]),
        is_veg=bool(raw.get("is_veg", False)),
        is_spicy=bool(raw.get("is_spicy", False)),
        is_available=bool(raw.get("is_available", True)),
        allergens=list(allergens),
        created_at=datetime.fromisoformat(str(raw["created_at"])) if raw.get("created_at") else None,
    )
