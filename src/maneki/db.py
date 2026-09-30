"""Supabase client singleton.

Provides a single async-safe client shared across the application.
Uses the service-role key — never exposed to the browser.
"""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from maneki.config import get_settings

_client_override: Client | None = None


def set_db_override(client: Client | None) -> None:
    global _client_override
    _client_override = client


def get_db() -> Client:
    """Return the active Supabase client.

    Supports test override via set_db_override().
    Initialised once on first call; subsequent calls return the same instance.
    The service-role key bypasses RLS — all data scoping is done in services/.
    """
    if _client_override is not None:
        return _client_override
    return _get_cached_db()


@lru_cache(maxsize=1)
def _get_cached_db() -> Client:
    cfg = get_settings()
    return create_client(cfg.supabase_url, cfg.supabase_service_key)
