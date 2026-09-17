# Data contracts

Where each shape is defined, and what is guaranteed about it.

| Contract | Defined by | Enforced by |
|---|---|---|
| Xero bill and purchase order | `schemas/xero_bill`, `schemas/xero_purchase_order` | Pydantic models with `extra="ignore"`, so an unexpected provider field is dropped rather than fatal |
| Chart of accounts | `xero_parsing.account_codes_from_accounts_response` | Refuses a numeric `Code` rather than coercing it, because coercion destroys leading zeroes |
| Reconciliation result | `policy_service/domain/reconciliation.ReconciliationResult` | `migrations/003` constraints, and a test asserting the SQL `CHECK` matches the enum |
| Model request | `claude_contract.build_request_payload` | A test asserting no identifier, amount, code or date appears in the serialised request |
| Model response | `schemas/semantic_review.model_facing` and `.strict` | The model-facing schema shapes the reply; the strict schema is applied server-side afterwards |
| Slack interaction | `schemas/slack_interaction` | Signature verification, then explicit parsing |
| Internal endpoints | `policy_service/api/runs.py` | Bearer auth, `Idempotency-Key`, body-size limit |

## Numbers

Every monetary and quantity value is a `Decimal`, parsed from the raw response
body with a parser configured to produce `Decimal` for every JSON number. A
float is refused at the boundary rather than converted, because by the time you
convert, the representation error is already there.

Variances are stored as exact decimal strings, so a reported difference is never
distorted by a float round trip on its way to a screen.

## Identifiers

Account codes are strings from parsing through persistence. Leading zeroes are
preserved and comparison is case-sensitive, because an account code is an
identifier rather than a number.

An ACCPAY bill has a single free-text field: the Xero UI labels it Reference and
the API returns it as `InvoiceNumber`. The supplier's invoice number and the
purchase-order reference therefore share it, split by
`normalisation.split_bill_reference`. This is a provider constraint rather than a
design choice, and it is recorded as such in
[`limitations-and-roadmap.md`](limitations-and-roadmap.md).
