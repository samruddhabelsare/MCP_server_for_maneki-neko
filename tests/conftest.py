"""Pytest configuration and shared fixtures.

Uses an in-memory fake Supabase so no network calls are made in tests.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from unittest.mock import MagicMock

import pytest

from maneki.config import get_settings

# ── Settings override ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def override_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject safe test values for all required settings."""
    test_env = {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_KEY": "test-service-key",
        "ADMIN_API_KEY": "test-admin-key",
        "INTERNAL_MCP_TOKEN": "test-internal-token",
    }
    for key, val in test_env.items():
        monkeypatch.setenv(key, val)

    # Clear lru_cache so each test gets fresh settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Fake Supabase ─────────────────────────────────────────────────────────────

class FakeQueryBuilder:
    """Minimal chainable fake for supabase-py query builders."""

    def __init__(self, store: list[dict[str, Any]]) -> None:
        self._store = store
        self._filters: list[tuple[str, Any]] = []
        self._limit: int | None = None
        self._data: list[dict[str, Any]] | None = None

    def select(self, *args: str, **kwargs: Any) -> FakeQueryBuilder:
        return self

    def insert(self, data: dict[str, Any] | list[dict[str, Any]]) -> FakeQueryBuilder:
        rows = data if isinstance(data, list) else [data]
        self._store.extend(rows)
        self._data = rows
        return self

    def update(self, data: dict[str, Any]) -> FakeQueryBuilder:
        for row in self._matching_rows():
            row.update(data)
        self._data = self._matching_rows()
        return self

    def delete(self) -> FakeQueryBuilder:
        to_delete = self._matching_rows()
        for row in to_delete:
            self._store.remove(row)
        self._data = to_delete
        return self

    def eq(self, col: str, val: Any) -> FakeQueryBuilder:
        self._filters.append((col, val))
        return self

    def limit(self, n: int) -> FakeQueryBuilder:
        self._limit = n
        return self

    def order(self, *args: Any, **kwargs: Any) -> FakeQueryBuilder:
        return self

    def maybe_single(self) -> FakeQueryBuilder:
        return self

    def single(self) -> FakeQueryBuilder:
        return self

    def gte(self, col: str, val: Any) -> FakeQueryBuilder:
        self._filters.append((col, ("gte", val)))
        return self

    def lte(self, col: str, val: Any) -> FakeQueryBuilder:
        self._filters.append((col, ("lte", val)))
        return self

    def ilike(self, col: str, val: str) -> FakeQueryBuilder:
        self._filters.append((col, ("ilike", val)))
        return self

    def _matching_rows(self) -> list[dict[str, Any]]:
        rows = self._store
        for col, cond in self._filters:
            if isinstance(cond, tuple) and len(cond) == 2:
                op, val = cond
                if op == "gte":
                    rows = [r for r in rows if r.get(col) is not None and r.get(col) >= val]
                elif op == "lte":
                    rows = [r for r in rows if r.get(col) is not None and r.get(col) <= val]
                elif op == "ilike":
                    pat = str(val).replace("%", "").lower()
                    rows = [r for r in rows if pat in str(r.get(col, "")).lower()]
            else:
                rows = [
                    r for r in rows
                    if r.get(col) == cond or str(r.get(col)) == str(cond)
                ]
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def execute(self) -> MagicMock:
        result = MagicMock()
        if self._data is not None:
            result.data = self._data
        else:
            result.data = self._matching_rows()
        return result


class FakeSupabase:
    """In-memory Supabase client for unit tests."""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def table(self, name: str) -> FakeQueryBuilder:
        return FakeQueryBuilder(self._tables[name])

    def rpc(self, name: str, params: dict[str, Any] | None = None) -> FakeQueryBuilder:
        # Tests that need RPC behaviour should patch this individually
        return FakeQueryBuilder([])

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Helper: pre-populate a table."""
        self._tables[table].extend(rows)

    def clear(self, table: str) -> None:
        self._tables[table].clear()


@pytest.fixture
def fake_db() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture(autouse=True)
def patch_db(fake_db: FakeSupabase) -> None:
    """Replace the real Supabase client with FakeSupabase for every test."""
    from maneki import db as db_module

    db_module.set_db_override(fake_db)  # type: ignore[arg-type]
    yield
    db_module.set_db_override(None)
