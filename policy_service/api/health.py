"""Liveness and readiness.

Liveness is independent of the database: a service that cannot reach Postgres
is not ready, but it is alive, and conflating the two makes the container
restart in a loop instead of reporting the real problem.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from policy_service.db import engine

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness() -> JSONResponse:
    if not engine.database_reachable():
        return JSONResponse(status_code=503, content={"error": "DATABASE_UNAVAILABLE"})

    missing = engine.missing_migrations()
    if missing:
        return JSONResponse(
            status_code=503,
            content={"error": "MIGRATIONS_NOT_READY", "missing": missing},
        )

    return JSONResponse(status_code=200, content={"status": "ready"})
