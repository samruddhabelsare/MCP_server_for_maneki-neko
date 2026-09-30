"""Pytest configuration and shared fixtures.

Uses an in-memory fake Supabase so no network calls are made in tests.
"""
from __future__ import annotations

import copy
import os
from collections import defaultdict
from typing import Any
from unittest.mock import MagicMock

import pytest

# Ensure safe default environment variables exist during module collection
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("INTERNAL_MCP_TOKEN", "test-internal-token")

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

    def order(self, col: str, desc: bool = False, **kwargs: Any) -> FakeQueryBuilder:
        self._store.sort(key=lambda r: str(r.get(col, "")), reverse=desc)
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
        if name == "confirm_draft" and params:
            sid = str(params["p_session_id"])
            p_items = params.get("p_items", [])
            p_total = params.get("p_total", 0.0)

            # Find draft
            drafts = [
                d for d in self._tables["order_drafts"]
                if str(d.get("session_id")) == sid and d.get("status") in ("open", "confirmed")
            ]
            if not drafts:
                return FakeQueryBuilder([])
            draft = drafts[0]

            # Idempotency: return existing order if already confirmed
            if draft.get("order_id"):
                existing = [o for o in self._tables["orders"] if str(o.get("id")) == str(draft["order_id"])]
                return FakeQueryBuilder(existing)

            # Find session
            sessions = [s for s in self._tables["sessions"] if str(s.get("id")) == sid]
            session = sessions[0] if sessions else {}

            from datetime import UTC, datetime
            from uuid import uuid4
            order_id = str(uuid4())
            order_row = {
                "id": order_id,
                "restaurant_id": draft.get("restaurant_id"),
                "customer_id": session.get("customer_id"),
                "table_number": session.get("table_number", 1),
                "items": p_items,
                "total_amount": p_total,
                "status": "pending",
                "payment_method": "cash",
                "customer_phone": None,
                "bot_id": None,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._tables["orders"].append(order_row)
            draft["status"] = "confirmed"
            draft["order_id"] = order_id
            return FakeQueryBuilder([order_row])

        return FakeQueryBuilder([])

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Helper: pre-populate a table.

        Deep-copies rows so that in-place updates during tests do not mutate
        module-level fixture data (SAMPLE_MENU, etc.) shared across test functions.
        """
        self._tables[table].extend(copy.deepcopy(rows))

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
