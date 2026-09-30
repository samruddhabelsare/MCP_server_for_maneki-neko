"""M0 smoke test: config fails fast on missing env, healthz reachable."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """App config must raise if required env vars are absent."""
    from maneki.config import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_MCP_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        get_settings()

    get_settings.cache_clear()


def test_healthz_returns_200() -> None:
    """GET /healthz must return 200 (or 503 for DB) — never 4xx/5xx from code."""
    from maneki.main import app

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "version" in body
