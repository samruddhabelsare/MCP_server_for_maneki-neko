"""ASGI application entry point.

Mounts:
  - /mcp/customer  — customer-scoped MCP server (session-bound)
  - /mcp/admin     — admin/kitchen MCP server (admin-key protected)
  - REST routes    — /healthz and all M1+ endpoints

Fails fast at startup if required env vars are missing.

Phase 1: Shared long-lived httpx.AsyncClient on app.state.
Phase 3: Warm menu cache on startup for all restaurants.
         Reconfigure cache TTLs from settings.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from maneki.admin_api import router as admin_router
from maneki.api import router as rest_router
from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import ManekiError
from maneki.mcp_admin import mcp_admin
from maneki.mcp_customer import mcp_customer
from maneki.services.menu import load_menu

# ── Fail fast: validate config before anything else ─────────────────────────
try:
    _cfg = get_settings()
except Exception as exc:  # pydantic ValidationError
    print(f"[FATAL] Missing or invalid configuration: {exc}", file=sys.stderr)
    sys.exit(1)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=_cfg.log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("maneki.main")

# ── MCP Sub-apps ─────────────────────────────────────────────────────────────
# Create streamable HTTP sub-apps before mounting and lifespan
customer_mcp_app = mcp_customer.streamable_http_app()
admin_mcp_app = mcp_admin.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage lifecycle for FastMCP session managers and shared HTTP client."""
    cfg = get_settings()

    # Phase 3: Reconfigure cache TTLs from settings
    from maneki import cache as cache_module  # noqa: PLC0415
    cache_module.reconfigure(
        menu_ttl=cfg.menu_cache_ttl,
        session_ttl=cfg.session_cache_ttl,
        profile_ttl=cfg.profile_cache_ttl,
        popularity_ttl=cfg.popularity_cache_ttl,
    )

    # Phase 1: Create long-lived shared HTTP client with keep-alive
    # Use HTTP/2 only if the h2 package is installed
    http2_enabled = False
    try:
        import h2  # noqa: F401
        http2_enabled = True
    except ImportError:
        pass

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=5.0),
        limits=httpx.Limits(
            max_keepalive_connections=20,
            max_connections=40,
            keepalive_expiry=30,
        ),
        http2=http2_enabled,
    )
    app.state.http_client = http_client
    logger.info('"Shared httpx client created (http2=%s)"', http2_enabled)

    # Phase 3: Warm menu cache on startup for all active restaurants
    try:
        def _warm_caches() -> None:
            try:
                db = get_db()
                res = db.table("restaurants").select("id").execute()
                rows = cast(list[dict[str, Any]], res.data) if res.data else []
                for r in rows:
                    if isinstance(r, dict) and r.get("id"):
                        rid = str(r["id"])
                        try:
                            load_menu(rid, include_unavailable=False)
                            logger.info('"Menu cache warmed for restaurant %s"', rid)
                        except Exception as exc:
                            logger.warning('"Failed to warm menu cache for %s: %s"', rid, exc)
            except Exception as exc:
                logger.warning('"Menu cache warm-up failed: %s"', exc)

        await asyncio.to_thread(_warm_caches)
    except Exception as exc:
        logger.warning('"Menu cache warm-up error: %s"', exc)

    async with mcp_customer.session_manager.run(), mcp_admin.session_manager.run():
        yield

    # Shutdown: close shared HTTP client
    await http_client.aclose()
    logger.info('"Shared httpx client closed"')


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Maneki Neko MCP Server",
    description="Multi-restaurant AI waiter backend — MCP + REST",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global error handler ──────────────────────────────────────────────────────
@app.exception_handler(ManekiError)
async def maneki_error_handler(request: Request, exc: ManekiError) -> JSONResponse:
    logger.warning('"ManekiError: %s %s"', exc.code, exc.message)
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception('"Unhandled error"')
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_error", "message": "An unexpected error occurred."},
    )

# ── REST routes ───────────────────────────────────────────────────────────────
app.include_router(rest_router)
app.include_router(admin_router)

# ── MCP sub-apps ─────────────────────────────────────────────────────────────
# Streamable HTTP transport — Inspector connects to these endpoints.
app.mount("/mcp/customer", customer_mcp_app)
app.mount("/mcp/admin",    admin_mcp_app)

logger.info('"Maneki Neko MCP Server started on port %s"', _cfg.port)
