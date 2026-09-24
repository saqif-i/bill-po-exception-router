"""Redaction, invariant I15.

Invariant I15: secrets are never written to logs, fixtures, workflow exports,
screenshots, evaluation data or Git.
"""

from __future__ import annotations

import json
import logging

import pytest

from policy_service.logging_config import JsonFormatter
from policy_service.security.redaction import REDACTED, redact


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "XERO_CLIENT_SECRET",
        "slack_bot_token",
        "Authorization",
        "anthropic_api_key",
        "apiKey",
        "signing_secret",
        "Cookie",
        "x_slack_signature",
        "private_key",
        "bearer_token",
    ],
)
def test_a_sensitive_key_never_survives(key) -> None:
    """Matched on a case-insensitive substring, so `clientSecret` and
    `xero_client_secret` are both caught."""
    assert redact({key: "the-actual-value"})[key] == REDACTED


def test_a_password_inside_a_connection_url_is_removed() -> None:
    """The key is `database_url`, which is not itself sensitive-looking."""
    out = redact({"database_url": "postgresql://bpr_app:hunter2@postgres:5432/bpr"})
    assert "hunter2" not in out["database_url"]
    assert "bpr_app" in out["database_url"]  # still diagnosable


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-abcdefghijklmnopqrstuvwx",
        "xoxb-1234567890-abcdefghijkl",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
    ],
)
def test_a_token_under_an_innocent_key_is_scrubbed(value) -> None:
    """A deny-list on key names would miss this entirely."""
    out = redact({"note": f"something {value} appeared"})
    assert value not in out["note"]
    assert REDACTED in out["note"]


def test_bulk_structures_are_omitted_not_logged() -> None:
    """A supplier's free text in a log file ends up in a screenshot."""
    out = redact({"bill_snapshot": {"LineItems": [{"Description": "widget"}]}})
    assert "widget" not in json.dumps(out)


def test_ordinary_values_survive_untouched() -> None:
    """Redaction that eats the diagnostics is redaction nobody keeps."""
    out = redact(
        {
            "correlation_id": "9f1c-4d2a",
            "exception_codes": ["QUANTITY_VARIANCE"],
            "run_id": "abc-123",
            "attempt": 2,
        }
    )
    assert out["correlation_id"] == "9f1c-4d2a"
    assert out["exception_codes"] == ["QUANTITY_VARIANCE"]
    assert out["attempt"] == 2


def test_nesting_is_bounded() -> None:
    deep: dict = {"a": {}}
    node = deep["a"]
    for _ in range(20):
        node["a"] = {}
        node = node["a"]
    node["password"] = "leak"
    assert "leak" not in json.dumps(redact(deep))


def test_the_input_is_never_mutated() -> None:
    original = {"password": "keep-me-here"}
    redact(original)
    assert original["password"] == "keep-me-here"


def test_a_long_string_is_truncated() -> None:
    out = redact({"note": "x" * 5000})
    assert len(out["note"]) < 700
    assert "truncated" in out["note"]


# --- the formatter, which is where redaction is actually enforced ----------
def _format(record_extra: dict, message: str = "something happened") -> dict:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    for key, value in record_extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_extras_are_redacted_by_the_formatter_not_the_call_site() -> None:
    """A call site that forgets is the normal case."""
    out = _format({"correlation_id": "abc", "slack_bot_token": "xoxb-secret-value"})
    assert out["correlation_id"] == "abc"
    assert out["slack_bot_token"] == REDACTED


def test_an_exception_logs_its_type_and_message_but_no_traceback() -> None:
    """A traceback carries local variables, and locals are where credentials
    live."""
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, None)
    try:
        raise ValueError("token sk-ant-abcdefghijklmnopqrstuv rejected")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(record))
    assert out["error"]["type"] == "ValueError"
    assert "sk-ant-" not in out["error"]["message"]
    assert "Traceback" not in json.dumps(out)


def test_every_line_is_valid_json() -> None:
    """A log line that is not parseable is a log line nobody can query."""
    line = JsonFormatter().format(
        logging.LogRecord("t", logging.INFO, __file__, 1, 'quotes " and \\ backslash', None, None)
    )
    assert json.loads(line)["message"]
