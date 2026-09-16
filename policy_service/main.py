"""Application entry point.

Startup validates configuration before the first request is accepted and fails
the process on any invalid setting (Volume 02 section 9.2).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from policy_service.api import errors, health, runs
from policy_service.api.limits import enforce_body_limit
from policy_service.config import get_settings
from policy_service.db import engine
from policy_service.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # raises on any invalid setting
    configure_logging(settings.log_level)
    yield
    engine.close_pool()


app = FastAPI(title="Bill-to-PO exception router", lifespan=lifespan, docs_url=None)

app.add_exception_handler(errors.ServiceError, errors.service_error_handler)
app.add_exception_handler(Exception, errors.unhandled_error_handler)
app.include_router(health.router)
app.include_router(runs.router)


app.middleware("http")(enforce_body_limit)


@app.middleware("http")
async def correlation_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """I09: a correlation id supplied by unauthenticated external ingress is
    never trusted. The service generates its own and echoes it back."""
    request.state.correlation_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = request.state.correlation_id
    return response
