# Security and privacy

What this system holds, what leaves it, and what it refuses to do with either.

## Credentials

| Credential | Held by | Reaches |
|---|---|---|
| PostgreSQL bootstrap superuser | The container init script, once | Nothing else. Never the service, the seeder or n8n |
| `bpr_owner` | `make migrate` only | Migrations. Never the running service |
| `bpr_app` | The policy service | The application database. No DDL, no DELETE on any immutable table |
| `bpr_retention` | Nothing in this build | Reserved. No retention process runs |
| `n8n_app` | n8n | Its own database only, proven by `make check-db` in CI on every push |
| Internal bearer token | n8n and the service | The service's own endpoints |
| Xero client id and secret | The service | `identity.xero.com` for a token, then read-only Accounting endpoints |
| Anthropic API key | The service | `api.anthropic.com` |
| Slack bot token and signing secret | The service | `slack.com`, and inbound verification |

n8n holds **one** application secret and no database credential. That is ADR-002,
and it is asserted continuously rather than assumed: `scripts/check_db_boundaries.py`
connects as `n8n_app` and confirms the refusal.

## What leaves the machine

Three destinations, and the payload to each is bounded.

**Xero.** Read requests only. The transport allow-list contains four GET
operations and no write, and a test asserts every entry is a GET.

**Anthropic.** Exactly two fields: a purchase-order line description and a bill
line description, both normalised. Absent by design and asserted absent by a
test: supplier name, any identifier, invoice or purchase-order number, tenant
id, correlation id, any monetary amount, quantity, account code, tax type, date,
exception code or deterministic result.

**Slack.** The triage card. It carries the invoice number, exception codes, the
routing destination and, where one exists, a wording recommendation with its
evidence spans. Evidence spans are verbatim substrings of the two descriptions,
so nothing is shown that the supplier did not write.

## Redaction

Applied in the log **formatter**, not at each call site, because a call site
that forgets is the normal case.

Keys matching `password`, `secret`, `token`, `authorization`, `api_key`,
`credential`, `signature`, `cookie`, `private` or `bearer` have their values
replaced. Bulk structures such as snapshots and line items are omitted rather
than logged. Values are additionally scrubbed for Anthropic keys, Slack tokens,
bearer literals and passwords embedded in connection URLs, which catches a
credential that arrives under an innocent key.

Exceptions log their type and message only. A traceback carries local variables,
and locals are where credentials live.

## Provider questions, answered before the flag was turned on

Volume 08 requires these to be recorded before `SEMANTIC_REVIEW_ENABLED` may be
set true. Fill them in from the provider's current documentation and your own
console, and **date them**.

| Question | Answer | Source | Checked |
|---|---|---|---|
| Exact model id in use | `claude-haiku-4-5-20251001` | `.env`, `SEMANTIC_MODEL_ID` | `<<DATE>>` |
| Provider's data-retention position for API traffic | `<<SUMMARY>>` | `<<URL>>` | `<<DATE>>` |
| Is this account excluded from retention | `<<YES_OR_NO>>` | console | `<<DATE>>` |
| Is API traffic used for training | `<<ANSWER>>` | `<<URL>>` | `<<DATE>>` |
| Cost basis | `<<PER_MTOK_IN/OUT>>` | `<<URL>>` | `<<DATE>>` |

**Until this table is filled in, leave the flag off.** Shipping default-off is
the honest position for a control whose compliance preconditions are unchecked,
and turning it on before checking would make invariant I19 a claim rather than a
fact.

## What this document does not claim

No compliance conclusion. No assertion about any organisation's internal
architecture, systems, processes or roadmap. That is invariant I23, and it is
the reason this file describes only what the code does.

All data is synthetic and builder-created, in a Xero demo company (I16).
