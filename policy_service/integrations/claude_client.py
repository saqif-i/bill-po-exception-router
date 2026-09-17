"""The Anthropic call.

Volume 08 sections 9.6 and 9.13, and ADR-003: the call is made here, in the
service, not from an n8n HTTP node. Three reasons. Validation runs in tested
in-process code before anything is persisted as usable. The attempt-start record
is committed before the call, so an unrecorded call is detectable. And the
prompt lives in a versioned file rather than a workflow canvas field, so a
prompt change is a reviewable diff.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import httpx

from policy_service.integrations.claude_contract import (
    MODEL_FACING_SCHEMA,
    Rejection,
    RejectionReason,
    build_request_payload,
    render_user_message,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
PROMPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "prompts"
PROMPT_VERSION = "line_semantics.v1"

TOOL_NAME = "record_semantic_review"


@dataclass(frozen=True)
class ProviderResult:
    """Either a raw parsed object, or a rejection. Never a partial answer."""

    raw: object | None = None
    rejection: Rejection | None = None
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    stop_reason: str = ""


def load_prompt() -> str:
    return (PROMPT_PATH / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


@dataclass
class ClaudeClient:
    api_key: str
    model: str
    timeout_seconds: float = 30.0
    max_tokens: int = 1024
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, verify=True)

    def review(
        self, *, purchase_order_line_description: str, bill_line_description: str
    ) -> ProviderResult:
        payload = build_request_payload(
            purchase_order_line_description=purchase_order_line_description,
            bill_line_description=bill_line_description,
        )
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": load_prompt(),
            "messages": [{"role": "user", "content": render_user_message(payload)}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Record a bounded recommendation about wording.",
                    "input_schema": MODEL_FACING_SCHEMA,
                }
            ],
            # Forcing the tool guarantees the SHAPE of the reply. It does not
            # guarantee the values, which is what the strict schema is for.
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }

        try:
            response = self._client.post(
                API_URL,
                json=body,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
        except httpx.TimeoutException:
            return ProviderResult(
                rejection=Rejection(RejectionReason.MALFORMED, "timeout"),
                model=self.model,
            )
        except httpx.HTTPError as exc:
            return ProviderResult(
                rejection=Rejection(RejectionReason.MALFORMED, f"transport: {type(exc).__name__}"),
                model=self.model,
            )

        if response.status_code >= 400:
            return ProviderResult(
                rejection=Rejection(RejectionReason.MALFORMED, f"provider {response.status_code}"),
                model=self.model,
            )

        data = response.json()
        stop_reason = str(data.get("stop_reason", ""))

        # Truncation is its own outcome. A half-written recommendation is not a
        # weaker recommendation, it is not a recommendation.
        if stop_reason == "max_tokens":
            return ProviderResult(
                rejection=Rejection(RejectionReason.TRUNCATED, stop_reason),
                model=data.get("model", self.model),
                stop_reason=stop_reason,
            )
        if stop_reason == "refusal":
            return ProviderResult(
                rejection=Rejection(RejectionReason.REFUSED, stop_reason),
                model=data.get("model", self.model),
                stop_reason=stop_reason,
            )
        if stop_reason not in ("tool_use", "end_turn", "stop_sequence"):
            return ProviderResult(
                rejection=Rejection(RejectionReason.UNEXPECTED_STOP, stop_reason),
                model=data.get("model", self.model),
                stop_reason=stop_reason,
            )

        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                return ProviderResult(
                    raw=block.get("input"),
                    model=data.get("model", self.model),
                    stop_reason=stop_reason,
                )

        return ProviderResult(
            rejection=Rejection(RejectionReason.MALFORMED, "no tool_use block"),
            model=data.get("model", self.model),
            stop_reason=stop_reason,
        )
