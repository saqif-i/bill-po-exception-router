"""Slack request signature verification (Volume 09 section 9.2).

Two separate protections, and they are not the same thing.

The SIGNATURE proves the request came from something holding your signing
secret. The TIMESTAMP window stops a valid, correctly signed request being
captured and replayed later. A signature alone is replayable forever.
"""

from __future__ import annotations

import hashlib
import hmac
import time

VERSION = "v0"
# Slack's own guidance. A captured request older than this is refused even
# though its signature is still perfectly valid.
MAX_AGE_SECONDS = 60 * 5


def compute_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    """The base string is version, timestamp and the RAW body, colon separated.

    Raw, not re-serialised. Parsing and re-encoding the JSON changes the bytes
    and the signature stops matching, which is the single most common reason
    this fails for people.
    """
    base = b":".join([VERSION.encode(), timestamp.encode(), body])
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{VERSION}={digest}"


def verify(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
) -> tuple[bool, str]:
    """Returns (ok, reason). The reason is for logs, never for the response."""
    if not timestamp or not signature:
        return False, "MISSING_SIGNATURE_HEADERS"

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False, "MALFORMED_TIMESTAMP"

    current = now if now is not None else time.time()
    # Absolute difference, so a clock skewed into the future is refused too.
    if abs(current - sent_at) > MAX_AGE_SECONDS:
        return False, "TIMESTAMP_OUTSIDE_WINDOW"

    expected = compute_signature(signing_secret, timestamp, body)
    # Constant time. `==` on a signature leaks a prefix through timing, which
    # is enough to forge one given patience.
    if not hmac.compare_digest(expected, signature):
        return False, "SIGNATURE_MISMATCH"

    return True, "OK"
