"""Slack request signature verification.

Pure, so no Slack workspace is needed.

The card wording is tested separately in test_slack_card.py, because it belongs
to a different module and arrives a stage later.
"""

from __future__ import annotations

import time

import pytest

from policy_service.security.hmac_verify import (
    MAX_AGE_SECONDS,
    compute_signature,
    verify,
)

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"


def signed(now: float, body: bytes = BODY, secret: str = SECRET):
    timestamp = str(int(now))
    return timestamp, compute_signature(secret, timestamp, body)


# --- the signature ---------------------------------------------------------
def test_a_correctly_signed_request_is_accepted() -> None:
    now = time.time()
    timestamp, signature = signed(now)
    ok, reason = verify(
        signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY, now=now
    )
    assert ok and reason == "OK"


def test_a_tampered_body_is_rejected() -> None:
    now = time.time()
    timestamp, signature = signed(now)
    ok, reason = verify(
        signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY + b"x", now=now
    )
    assert not ok and reason == "SIGNATURE_MISMATCH"


def test_the_wrong_secret_is_rejected() -> None:
    now = time.time()
    timestamp, signature = signed(now, secret="a" * 32)
    ok, _ = verify(
        signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY, now=now
    )
    assert not ok


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [
        (None, "v0=abc"),
        ("123", None),
        (None, None),
    ],
)
def test_missing_headers_are_rejected(timestamp, signature) -> None:
    ok, reason = verify(signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY)
    assert not ok and reason == "MISSING_SIGNATURE_HEADERS"


def test_a_malformed_timestamp_is_rejected() -> None:
    ok, reason = verify(
        signing_secret=SECRET, timestamp="not-a-number", signature="v0=abc", body=BODY
    )
    assert not ok and reason == "MALFORMED_TIMESTAMP"


# --- replay protection, which the signature alone does not give you --------
def test_an_old_but_correctly_signed_request_is_rejected() -> None:
    """A signature stays valid forever. Without the window, a captured request
    could be replayed at any time and would verify perfectly."""
    now = time.time()
    old = now - (MAX_AGE_SECONDS + 60)
    timestamp, signature = signed(old)
    ok, reason = verify(
        signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY, now=now
    )
    assert not ok and reason == "TIMESTAMP_OUTSIDE_WINDOW"


def test_a_request_from_the_future_is_rejected_too() -> None:
    """The window is absolute, so a skewed clock cannot buy extra validity."""
    now = time.time()
    future = now + (MAX_AGE_SECONDS + 60)
    timestamp, signature = signed(future)
    ok, reason = verify(
        signing_secret=SECRET, timestamp=timestamp, signature=signature, body=BODY, now=now
    )
    assert not ok and reason == "TIMESTAMP_OUTSIDE_WINDOW"


def test_the_comparison_is_constant_time() -> None:
    source = __import__("pathlib").Path("policy_service/security/hmac_verify.py").read_text()
    assert "compare_digest" in source
    assert "== signature" not in source
