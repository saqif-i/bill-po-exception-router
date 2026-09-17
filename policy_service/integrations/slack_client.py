"""One Slack client, in the service (ADR-004).

The service must hold the signing secret to verify inbound interactions, so
splitting posting from receiving would put Slack credentials in two places for
no gain.

Both calls are made directly rather than from an outbox worker, which makes the
card update best-effort. That is stated in BUILD-SCOPE-v1.md and is not claimed
to be otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

POST_MESSAGE = "https://slack.com/api/chat.postMessage"
UPDATE_MESSAGE = "https://slack.com/api/chat.update"


@dataclass(frozen=True)
class PostOutcome:
    ok: bool
    message_ts: str | None = None
    # The channel ID Slack resolved the name to. `chat.postMessage` accepts a
    # channel NAME; `chat.update` requires the ID. Posting by name and then
    # updating by name fails with `channel_not_found`, so the id is captured
    # here and stored.
    channel_id: str | None = None
    error: str = ""
    dispatch_unknown: bool = False


@dataclass
class SlackClient:
    bot_token: str
    timeout_seconds: float = 10.0
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, verify=True)

    def _call(self, url: str, payload: dict) -> PostOutcome:
        try:
            response = self._client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except httpx.TimeoutException:
            # The request may or may not have been delivered. Reporting this as
            # a failure would be a claim we cannot support, so it is reported as
            # unknown and a duplicate card becomes possible rather than silent.
            return PostOutcome(False, error="TIMEOUT", dispatch_unknown=True)
        except httpx.HTTPError as exc:
            return PostOutcome(False, error=type(exc).__name__, dispatch_unknown=True)

        if response.status_code != 200:
            return PostOutcome(False, error=f"HTTP_{response.status_code}")

        body = response.json()
        if not body.get("ok"):
            # A named Slack error is a definite failure: the request arrived and
            # was rejected, so nothing was posted.
            return PostOutcome(False, error=str(body.get("error", "unknown")))
        return PostOutcome(True, message_ts=body.get("ts"), channel_id=body.get("channel"))

    def post_card(self, *, channel: str, blocks: list[dict], text: str) -> PostOutcome:
        """`text` is the notification fallback, shown in the sidebar and on a
        watch. A card with no fallback is unreadable in a notification."""
        return self._call(POST_MESSAGE, {"channel": channel, "blocks": blocks, "text": text})

    def update_card(
        self, *, channel: str, message_ts: str, blocks: list[dict], text: str
    ) -> PostOutcome:
        return self._call(
            UPDATE_MESSAGE,
            {"channel": channel, "ts": message_ts, "blocks": blocks, "text": text},
        )
