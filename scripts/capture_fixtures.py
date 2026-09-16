#!/usr/bin/env python3
"""Capture real Xero responses to disk, once (ADR-008).

Reconciliation takes two JSON documents and does not care where they came from.
Capturing real responses on day one means the rest of the build runs offline:
no rate limits, no token expiry, no 28-day demo reset, and tests that are fast
and deterministic.

Run this after the fixtures exist in the demo company. Run it again only if you
change a fixture.

    python scripts/capture_fixtures.py
"""

from __future__ import annotations

import pathlib
import sys

import httpx

from policy_service.config import get_settings
from policy_service.integrations import xero_parsing
from policy_service.integrations.xero_auth import (
    TokenCache,
    XeroCredentials,
    request_token,
)
from policy_service.integrations.xero_client import XeroReadClient

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _write(relative: str, payload: object) -> pathlib.Path:
    path = OUT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    # Decimals are written as exact strings, never as floats.
    path.write_text(xero_parsing.dumps(payload) + "\n", encoding="utf-8")
    return path


def main() -> int:
    settings = get_settings()
    if not settings.xero_client_id or not settings.xero_client_secret:
        print("XERO_CLIENT_ID and XERO_CLIENT_SECRET are required", file=sys.stderr)
        return 2

    credentials = XeroCredentials(
        client_id=settings.xero_client_id,
        client_secret=settings.xero_client_secret,
        scopes=settings.xero_scopes,
    )
    cache = TokenCache()
    http = httpx.Client(timeout=30.0)

    def token() -> str:
        return cache.get(lambda: request_token(http, credentials))

    client = XeroReadClient(
        token_provider=token,
        connect_timeout=settings.xero_connect_timeout_seconds,
        read_timeout=settings.xero_read_timeout_seconds,
    )

    # Refuse to capture from anything that is not the demo company. A capture
    # taken from a real organisation would put real supplier data on disk and
    # into a public repository (I16).
    org = client.get_organisation()
    name = org["Organisations"][0]["Name"]
    if settings.xero_expected_org_name.lower() not in name.lower():
        print(
            f"refusing to capture: connected organisation is {name!r}, "
            f"expected something containing {settings.xero_expected_org_name!r}",
            file=sys.stderr,
        )
        return 1
    print(f"connected to {name}")

    written: list[pathlib.Path] = []
    written.append(_write("organisation.json", org))

    accounts = client.list_accounts()
    written.append(_write("accounts.json", accounts))
    codes = xero_parsing.account_codes_from_accounts_response(accounts)
    print(f"chart of accounts: {len(codes)} codes")

    bills = client.list_invoices(modified_since=None, max_records=100)
    seeded = [
        invoice
        for invoice in bills.get("Invoices", [])
        if str(invoice.get("InvoiceNumber", "")).upper().startswith("INV-100")
    ]
    print(f"bills: {len(bills.get('Invoices', []))} returned, {len(seeded)} look seeded")

    for invoice in seeded:
        number = invoice["InvoiceNumber"]
        written.append(_write(f"bills/{number}.json", {"Invoices": [invoice]}))

        reference = str(invoice.get("Reference") or "").strip()
        if not reference:
            continue
        try:
            order = client.get_purchase_order(reference)
        except Exception as exc:
            print(f"  {number}: no purchase order for {reference!r} ({type(exc).__name__})")
            continue
        written.append(_write(f"purchase_orders/{reference}.json", order))

    print(f"\nwrote {len(written)} fixture files under {OUT}")
    for path in written:
        print(f"  {path.relative_to(OUT.parent.parent)}")
    print(
        "\nThese contain synthetic demo-company data only and ARE committed, "
        "so the test suite runs without a Xero connection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
