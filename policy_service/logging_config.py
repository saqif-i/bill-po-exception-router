"""Structured logging, with redaction applied on the way out.

Volume 02 section 9.7.

Redaction happens in the **formatter**, not at each call site. A call site that
forgets is the normal case, and a redactor you have to remember to call is a
redactor that eventually is not called.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from policy_service.security.redaction import redact

# Attributes the standard library puts on every record. Anything else was added
# by a caller and is treated as structured context.
_STANDARD = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        if extras:
            payload |= redact(extras)

        if record.exc_info:
            # The type and message only. A traceback can carry local variables
            # into the log, and locals are where credentials live.
            exc_type, exc_value, _ = record.exc_info
            payload["error"] = {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": redact(str(exc_value)),
            }

        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
