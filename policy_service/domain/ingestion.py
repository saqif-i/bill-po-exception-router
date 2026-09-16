"""Turning a Xero bill into a run.

Two jobs: derive the ingestion version key, and enforce the fixture allow-list.

The allow-list check happens HERE, in the service, not on the n8n canvas
(Volume 07 section 9.4). A record failing it never becomes a run. On the first
poll against a Demo Company this is the difference between nine runs and
ninety, because the demo company arrives pre-populated with sample invoices.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from policy_service.domain.models import Bill
from policy_service.domain.normalisation import split_bill_reference
from policy_service.integrations.xero_parsing import dumps


def ingestion_version_key(bill: Bill) -> str | None:
    """Identifies a specific version of a bill's content.

    Re-polling an unchanged bill produces the same key, so uq_runs_ingestion_
    version makes the deliberate poll-window overlap harmless: no second run.
    A genuine edit in Xero changes the content, changes the key, and produces a
    new run rather than mutating the old one.

    UpdatedDateUTC alone is not enough: it is provider metadata, and a run must
    be reproducible from the content it actually reconciled.
    """
    if bill.invoice_id is None:
        return None
    material = {
        "invoice_id": str(bill.invoice_id),
        # Split, so a change to only the PO half still changes the key.
        "invoice_number": split_bill_reference(bill.invoice_number)[0],
        "po_reference": split_bill_reference(bill.invoice_number)[1] or "",
        "currency": (bill.currency_code or "").strip().upper(),
        "total": str(bill.total) if bill.total is not None else None,
        "total_tax": str(bill.total_tax) if bill.total_tax is not None else None,
        "lines": [
            {
                "description": (line.description or "").strip(),
                "item_code": (line.item_code or "").strip(),
                "account_code": (line.account_code or "").strip(),
                "tax_type": (line.tax_type or "").strip(),
                "quantity": str(line.quantity) if line.quantity is not None else None,
                "unit_amount": (str(line.unit_amount) if line.unit_amount is not None else None),
                "line_amount": (str(line.line_amount) if line.line_amount is not None else None),
                "tax_amount": (str(line.tax_amount) if line.tax_amount is not None else None),
            }
            for line in bill.line_items
        ],
    }
    digest = hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()
    return f"v1:{digest[:32]}"


def bill_hash(bill: Bill) -> str:
    key = ingestion_version_key(bill)
    return key.split(":", 1)[1] if key else ""


@dataclass(frozen=True)
class AllowListDecision:
    allowed: bool
    fixture_id: str | None = None
    reason: str = ""


def check_allow_list(resource_id: str, active_fixtures: dict[str, str]) -> AllowListDecision:
    """I14: only records on the ACTIVE fixture allow-list may become runs.

    `active_fixtures` maps a Xero resource id to a fixture id, loaded from
    seed_fixtures where fixture_status = 'ACTIVE'. A superseded or retired
    fixture is not in the map, so its records stop becoming runs immediately.
    """
    fixture_id = active_fixtures.get(str(resource_id))
    if fixture_id is None:
        return AllowListDecision(False, None, "NOT_ON_ACTIVE_ALLOW_LIST")
    return AllowListDecision(True, fixture_id, "")
