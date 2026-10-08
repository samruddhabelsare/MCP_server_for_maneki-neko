"""Small thread-safe TTL cache for hot data.

Used for:
  - Menu items + categories per restaurant  (MENU_CACHE_TTL=45 s)
  - Session row including expires_at         (SESSION_CACHE_TTL=30 s)
  - Customer profile                         (PROFILE_CACHE_TTL=120 s)
  - Popularity scores per restaurant         (POPULARITY_CACHE_TTL=300 s)
  - Static part of the system prompt         (no TTL — invalidated explicitly)

Design:
  - Thread-safe via threading.Lock (safe from asyncio.to_thread workers).
  - Monotonic clock so wall-clock changes don't affect TTL.
  - Max-size eviction (LRU-style: evict the oldest entry when full).
  - Hit/miss counters exposed for timing logs.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Thread-safe TTL cache with bounded size and LRU eviction."""

    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._store: OrderedDict[str, tuple[V, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, key: str) -> V | None:
        """Return cached value if present and not expired, else None."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, exp = entry
            if time.monotonic() > exp:
                del self._store[key]
                self.misses += 1
                return None
            # Move to end (LRU touch)
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: V, ttl: float | None = None) -> None:
        """Store a value with the given (or default) TTL."""
        exp = time.monotonic() + (ttl if ttl is not None else self._ttl)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, exp)
            # Evict oldest if over capacity
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Remove a single key."""
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove all keys starting with `prefix`."""
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]

    def clear(self) -> None:
        """Remove all entries and reset counters."""
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, Any]:
        """Return hit/miss counters (for timing logs)."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "size": len(self._store),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ── Singleton caches ──────────────────────────────────────────────────────────
# These are module-level so they live for the process lifetime.

#: Menu items (list[MenuItem]) keyed by f"menu:{restaurant_id}"
#: Categories (list[str]) keyed by f"cats:{restaurant_id}"
menu_cache: TTLCache[Any] = TTLCache(ttl=45, maxsize=256)

#: Session rows keyed by f"session:{session_id}"
session_cache: TTLCache[Any] = TTLCache(ttl=30, maxsize=1024)

#: Customer profile dicts keyed by f"profile:{customer_id}:{restaurant_id}"
profile_cache: TTLCache[Any] = TTLCache(ttl=120, maxsize=1024)

#: Popularity Counter keyed by f"popularity:{restaurant_id}"
popularity_cache: TTLCache[Any] = TTLCache(ttl=300, maxsize=256)

#: Static system-prompt prefix keyed by f"prompt:{character}:{restaurant_id}"
#: (No TTL: invalidated when menu/character changes)
prompt_cache: TTLCache[str] = TTLCache(ttl=3600, maxsize=64)


def reconfigure(
    menu_ttl: int = 45,
    session_ttl: int = 30,
    profile_ttl: int = 120,
    popularity_ttl: int = 300,
) -> None:
    """Reconfigure TTLs from settings (called at startup)."""
    menu_cache._ttl = float(menu_ttl)
    session_cache._ttl = float(session_ttl)
    profile_cache._ttl = float(profile_ttl)
    popularity_cache._ttl = float(popularity_ttl)
