"""Request-size and field-length limits.

A bounded request is the cheapest denial-of-service control there is, and it
also stops an oversized body from reaching the JSON parser at all.

Plain ASGI rather than an HTTP middleware function, because the limit has to
apply to the bytes actually received. A chunked request carries no
Content-Length, so checking the header alone let any size through.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 256 * 1024


class BodyLimitMiddleware:
    """Reads the body up to the limit before the app sees any of it.

    A declared Content-Length over the limit is refused without reading. A body
    that turns out larger than the limit, declared or not, is refused as soon
    as the limit is passed. Anything within it is replayed to the app unchanged,
    which the Slack endpoint relies on to verify the signature over the raw
    bytes.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._refuse(scope, send, 413, "REQUEST_TOO_LARGE")
                    return
            except ValueError:
                await self._refuse(scope, send, 400, "INVALID_CONTENT_LENGTH")
                return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._refuse(scope, send, 413, "REQUEST_TOO_LARGE")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(scope: Scope, send: Send, status: int, code: str) -> None:
        payload: dict = {"error": code}
        correlation_id = (scope.get("state") or {}).get("correlation_id")
        if correlation_id:
            payload["correlation_id"] = correlation_id
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
