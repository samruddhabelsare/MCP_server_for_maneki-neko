"""ASGI application entry point.

Mounts:
  - /mcp/customer  — customer-scoped MCP server (session-bound)
  - /mcp/admin     — admin/kitchen MCP server (admin-key protected)
  - REST routes    — /healthz and all M1+ endpoints

Fails fast at startup if required env vars are missing.
"""
from __future__ import annotations

import logging
import sys

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from maneki.api import router as rest_router
from maneki.config import get_settings
from maneki.errors import ManekiError
from maneki.mcp_admin import mcp_admin
from maneki.mcp_customer import mcp_customer

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
    """Manage lifecycle for FastMCP session managers."""
    async with mcp_customer.session_manager.run(), mcp_admin.session_manager.run():
        yield


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

# ── MCP sub-apps ─────────────────────────────────────────────────────────────
# Streamable HTTP transport — Inspector connects to these endpoints.
app.mount("/mcp/customer", customer_mcp_app)
app.mount("/mcp/admin",    admin_mcp_app)

logger.info('"Maneki Neko MCP Server started on port %s"', _cfg.port)
