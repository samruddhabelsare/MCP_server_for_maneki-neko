"""FastAPI REST routes — non-LLM endpoints.

Routes are registered in milestones M1-M4. This file sets up
the router and the /healthz liveness probe for M0.
"""
from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from maneki.db import get_db

router = APIRouter()

_start_time = time.time()


@router.get("/healthz", tags=["ops"])
async def healthz() -> JSONResponse:
    """Liveness probe — returns 200 if the service is up.

    Also checks Supabase connectivity so Render health checks
    catch DB misconfiguration early.
    """
    db_ok = False
    db_error: str | None = None

    try:
        # Lightweight ping: fetch 1 row from restaurants
        db = get_db()
        db.table("restaurants").select("id").limit(1).execute()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "supabase": "ok" if db_ok else f"error: {db_error}",
        "version": "0.1.0",
    }
    return JSONResponse(
        content=payload,
        status_code=200 if db_ok else 503,
    )


# M1+ routes are added here (sessions, orders, feedback, etc.)
