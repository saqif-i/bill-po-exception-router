"""Safe error shapes.

Volume 02 section 9.6: an error response carries a stable code and the
correlation id, and nothing else. No stack trace, no driver message, no SQL,
no configuration value.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(code or self.code)
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


def error_body(code: str, correlation_id: str | None) -> dict[str, str]:
    body = {"error": code}
    if correlation_id:
        body["correlation_id"] = correlation_id
    return body


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, correlation_id),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR", correlation_id))
