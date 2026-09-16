"""Request-size and field-length limits (Volume 02 section 9.5).

A bounded request is the cheapest denial-of-service control there is, and it
also stops an oversized body from reaching the JSON parser at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

MAX_BODY_BYTES = 256 * 1024


async def enforce_body_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "REQUEST_TOO_LARGE",
                        "correlation_id": getattr(request.state, "correlation_id", None),
                    },
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "INVALID_CONTENT_LENGTH"})
    return await call_next(request)
