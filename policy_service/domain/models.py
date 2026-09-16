"""Typed bill, purchase order and chart of accounts.

Decimals are parsed from strings. Account codes are strings from parsing
through persistence and are never coerced from a JSON number (section 9.5).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from policy_service.domain.normalisation import to_decimal


def _decimal(value: object) -> Decimal | None:
    return to_decimal(value)


Money = Annotated[Decimal | None, BeforeValidator(_decimal)]


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_item_id: str | None = Field(default=None, alias="LineItemID")
    description: str | None = Field(default=None, alias="Description")
    item_code: str | None = Field(default=None, alias="ItemCode")
    # A string or an explicitly absent value. Never a JSON number.
    account_code: str | None = Field(default=None, alias="AccountCode")
    tax_type: str | None = Field(default=None, alias="TaxType")
    quantity: Money = Field(default=None, alias="Quantity")
    unit_amount: Money = Field(default=None, alias="UnitAmount")
    line_amount: Money = Field(default=None, alias="LineAmount")
    tax_amount: Money = Field(default=None, alias="TaxAmount")


class Contact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contact_id: UUID | None = Field(default=None, alias="ContactID")
    name: str | None = Field(default=None, alias="Name")


class Bill(BaseModel):
    """An ACCPAY invoice."""

    model_config = ConfigDict(extra="ignore")

    invoice_id: UUID | None = Field(default=None, alias="InvoiceID")
    invoice_number: str | None = Field(default=None, alias="InvoiceNumber")
    type: str | None = Field(default=None, alias="Type")
    status: str | None = Field(default=None, alias="Status")
    # NOT used for ACCPAY. Xero returns Reference only for ACCREC invoices; on
    # a bill the UI field labelled "Reference" arrives as InvoiceNumber. Kept so
    # a payload carrying it does not fail validation.
    reference: str | None = Field(default=None, alias="Reference")
    currency_code: str | None = Field(default=None, alias="CurrencyCode")
    date: str | None = Field(default=None, alias="Date")
    contact: Contact | None = Field(default=None, alias="Contact")
    line_items: list[LineItem] = Field(default_factory=list, alias="LineItems")
    total_tax: Money = Field(default=None, alias="TotalTax")
    total: Money = Field(default=None, alias="Total")
    updated_date_utc: str | None = Field(default=None, alias="UpdatedDateUTC")


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    purchase_order_id: UUID | None = Field(default=None, alias="PurchaseOrderID")
    purchase_order_number: str | None = Field(default=None, alias="PurchaseOrderNumber")
    status: str | None = Field(default=None, alias="Status")
    currency_code: str | None = Field(default=None, alias="CurrencyCode")
    contact: Contact | None = Field(default=None, alias="Contact")
    line_items: list[LineItem] = Field(default_factory=list, alias="LineItems")
    total_tax: Money = Field(default=None, alias="TotalTax")
    total: Money = Field(default=None, alias="Total")


class Tolerances(BaseModel):
    """Volume 06 sections 9.6 and 9.7.

    Quantity and unit price default to ZERO tolerance. A supplier billing a
    different quantity or price is exactly what this system exists to surface.

    TOTAL_TOLERANCE_PCT is deliberately absent. At 0.5 percent a six-figure
    order would silently accept a 500 unit discrepancy. A fixed absolute
    tolerance covers accumulated cent-level rounding and nothing more.
    """

    model_config = ConfigDict(extra="forbid")

    quantity_abs: Decimal = Decimal("0")
    unit_price_abs: Decimal = Decimal("0.0000")
    line_amount_abs: Decimal = Decimal("0.01")
    tax_abs: Decimal = Decimal("0.02")
    total_abs: Decimal = Decimal("0.05")
