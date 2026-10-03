"""The request body limit applies to the bytes received, not the header."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from policy_service.api.limits import BodyLimitMiddleware

LIMIT = 1024


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, max_bytes=LIMIT)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        body = await request.body()
        return {"length": len(body), "head": body[:8].decode()}

    return TestClient(app)


def _chunks(total: int, size: int = 256):
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        yield b"x" * piece
        sent += piece


def test_a_chunked_body_over_the_limit_is_refused() -> None:
    """No Content-Length, so the old header check let this straight through."""
    response = _app().post("/echo", content=_chunks(LIMIT * 4))
    assert response.status_code == 413
    assert response.json()["error"] == "REQUEST_TOO_LARGE"


def test_a_chunked_body_within_the_limit_reaches_the_app_intact() -> None:
    response = _app().post("/echo", content=_chunks(LIMIT))
    assert response.status_code == 200
    assert response.json() == {"length": LIMIT, "head": "xxxxxxxx"}


def test_a_declared_length_over_the_limit_is_refused_without_reading() -> None:
    response = _app().post("/echo", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413


def test_an_unparseable_content_length_is_a_client_error() -> None:
    response = _app().post("/echo", content=b"x", headers={"content-length": "lots"})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_CONTENT_LENGTH"


def test_the_service_refusal_carries_the_correlation_id() -> None:
    from policy_service.api.limits import MAX_BODY_BYTES
    from policy_service.main import app

    response = TestClient(app).post("/runs/poll", content=_chunks(MAX_BODY_BYTES + 1, 65536))
    assert response.status_code == 413
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
