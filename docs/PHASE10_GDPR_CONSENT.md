# Phase 10 — GDPR / Marketing Consent Policy

**This document distinguishes technical capability from business/legal
policy throughout. Nothing here is legal advice; anywhere a legal
judgment is required, it is flagged explicitly as needing the store
owner's (or their advisor's) sign-off, not answered by this pipeline.**

## 1. What consent data actually exists (verified against the source, not assumed)

Three places were checked directly in `migration/sql/dump.sql`:

| Source | Rows | Finding |
|---|---:|---|
| `wp_wc_email_unsubscribes` (WooCommerce's own transactional-unsubscribe table) | 0 | Table exists but is empty — no signal |
| `wp_fc_subscribers` (FluentCRM, a separate email-marketing plugin) | 6,692 | Real signal: `status` = `subscribed` (6,437), `unsubscribed` (234), `pending` (21) |
| WooCommerce customer/order fields read by `database_parser.py` | — | **No marketing-consent field is read here at all** — confirmed by inspecting `CUSTOMER_META_KEYS`/`is_customer_meta_key()`, which only captures `wp_capabilities` and `billing_*`/`shipping_*` |

Cross-referenced against the 12,096 IMPORT-eligible customers
(`reports/phase10_customer_dry_run.py`, `marketing_consent_source`/
`marketing_consent_status` columns in `reports/phase10_customer_manifest.csv`):

| Classification | Count |
|---|---:|
| A. Explicit marketing consent (FluentCRM `subscribed`) | 6,295 |
| B. Explicit marketing opt-out (FluentCRM `unsubscribed`) | 229 |
| Explicit "in progress" (FluentCRM `pending`) | 21 |
| C. No consent information at all | 5,551 |
| D. Ambiguous/unknown | 0 — every record is either A, B, "pending," or C; nothing fell into a genuinely ambiguous middle state |
| E. Consent stored in a third-party plugin | FluentCRM, as above — this **is** category A/B/pending's source |
| F. Consent stored in custom metadata | None found beyond FluentCRM |

**5,551 of 12,096 customers (46%) have no consent signal anywhere.**

## 2. What this pipeline does NOT do

Per explicit instruction and as a matter of basic GDPR hygiene, consent
is never inferred from: having an account, having placed an order, the
mere existence of an email address, or any assumption about "newsletter
subscription" absent a real signal. **`docs/ARCHITECTURE.md`'s existing
customer-CSV code already independently reached the same default** —
`Accepts Email Marketing`/`Accepts SMS Marketing` are hardcoded to `no`
for every record — though that was a prior session's technical default,
never formally ratified as a store-owner-approved policy (see § 4).

## 3. Shopify's actual representation and its limits

Verified via live schema introspection against `CustomerEmailMarketingConsentInput`:

- `marketingState` accepts only `SUBSCRIBED`, `UNSUBSCRIBED`, or `PENDING`
  as **input**. `NOT_SUBSCRIBED` (the default for a customer with no
  consent field set at all), `REDACTED`, and `INVALID` are system-set
  and rejected if sent as input.
- `marketingOptInLevel`, `consentUpdatedAt`, and `sourceLocationId` are
  also available, letting a real historical consent timestamp/source be
  recorded if one exists — FluentCRM rows do carry timestamps, not yet
  extracted by this pipeline.
- There is no field to represent "unknown" as anything other than simply
  not setting `emailMarketingConsent` at all (→ Shopify default
  `NOT_SUBSCRIBED`).
- No SMS or WhatsApp consent data exists in any source table this
  pipeline reads, so `smsMarketingConsent`/`whatsAppMarketingConsent`
  have nothing to map from.

## 4. Proposed policy (NOT YET APPROVED — flagged for the store owner)

| Case | Proposed technical representation | Business/legal question this does NOT answer |
|---|---|---|
| FluentCRM `subscribed` (6,295) | Set `emailMarketingConsent.marketingState = SUBSCRIBED` | **Is FluentCRM's original opt-in mechanism (single opt-in? double opt-in? what disclosure was shown?) a legally sufficient basis to carry that consent forward into a *new* system (Shopify) under UK GDPR/PECR?** This pipeline has no visibility into how FluentCRM originally collected consent — that requires the store owner (or their data controller/DPO) to confirm, not something inferable from a database export. |
| FluentCRM `unsubscribed` (229) | Set `marketingState = UNSUBSCRIBED` | None — an explicit opt-out should always be honored |
| FluentCRM `pending` (21) | Omit the field (treat as unknown) — `PENDING` in FluentCRM means double opt-in confirmation was never completed, so there is no completed consent to carry forward | None — this is a safe default, not a business question |
| No signal at all (5,551) | Omit the field entirely (Shopify default `NOT_SUBSCRIBED`) | None — this is the safest possible default |

**Until the store owner explicitly approves the `SUBSCRIBED` row above,
the default position for every customer — including the 6,295 with a
FluentCRM `subscribed` record — is to omit `emailMarketingConsent`
entirely.** A pre-existing consent record in a marketing tool is real
evidence, but treating it as sufficient legal basis for a *different*
platform's marketing system is a decision this pipeline is not
authorized to make.

## 5. Explicit approval statement needed

> "I approve carrying forward FluentCRM's `subscribed` consent status
> (6,295 customers) into Shopify's `emailMarketingConsent` as
> `SUBSCRIBED`, and confirm this consent was validly obtained under
> applicable UK GDPR/PECR requirements."

Absent that statement, Phase 10 proceeds with `emailMarketingConsent`
omitted for every customer, full stop — the safe default, not a stall
tactic.
