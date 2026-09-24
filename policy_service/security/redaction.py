"""Redaction for anything that reaches a log, an event payload or a screenshot.

Invariant I15; see docs/security.md.

The design choice worth understanding: this is an **allow-list on keys** and a
pattern scrub on values, not a deny-list of known secret names. A deny-list
fails the moment someone adds a field nobody thought of, and the failure is
silent, which is the worst property a redactor can have.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose VALUE is never logged, matched case-insensitively on a substring
# so `xero_client_secret` and `clientSecret` are both caught.
SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "signature",
    "cookie",
    "private",
    "bearer",
)

# Structures that are large, supplier-written, or both. Logging them would put
# a supplier's free text into a log file that later goes into a screenshot.
BULK_KEY_PARTS = ("snapshot", "payload", "line_items", "lineitems", "blocks")

# Value patterns, for anything that slips through under an innocent key.
VALUE_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    # A password inside a connection URL.
    re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@/\s]+(@)"),
)

MAX_STRING = 512
MAX_DEPTH = 6


def _scrub_value(value: str) -> str:
    for pattern in VALUE_PATTERNS:
        if pattern.groups:
            value = pattern.sub(r"\1" + REDACTED + r"\2", value)
        else:
            value = pattern.sub(REDACTED, value)
    if len(value) > MAX_STRING:
        value = value[:MAX_STRING] + f"...[truncated {len(value) - MAX_STRING}]"
    return value


def redact(value: Any, *, depth: int = 0) -> Any:
    """Return a copy safe to log. The input is never mutated.

    Depth is bounded because a deeply nested provider payload should not be able
    to make logging expensive, and because anything that deep is not readable in
    a log line anyway.
    """
    if depth > MAX_DEPTH:
        return "[TRUNCATED_DEPTH]"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                out[key] = REDACTED
            elif any(part in lowered for part in BULK_KEY_PARTS):
                out[key] = f"[{type(inner).__name__} omitted]"
            else:
                out[key] = redact(inner, depth=depth + 1)
        return out

    if isinstance(value, list | tuple):
        return [redact(item, depth=depth + 1) for item in value[:20]]

    if isinstance(value, str):
        return _scrub_value(value)

    return value
