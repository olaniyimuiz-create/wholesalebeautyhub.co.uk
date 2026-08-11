# Phase 10 — Customer Import Approval Gate

Formal authorization request. **Nothing in this document is that
authorization.** No customer was created, updated, deleted, merged, or
queried by GID in Shopify while producing it.

## 1. Source customer count

13,043 raw `wp_wc_customer_lookup` rows (independently recomputed from
`migration/sql/dump.sql`, not inherited from `customers.json`).

## 2. Eligible customer count

**12,096 IMPORT-eligible.**

## 3. Quarantine count

**539** — 292 missing email, 247 conflicting identity sharing one email
(`reports/phase10_customer_quarantine.csv`, every row carries a
recommended human action).

## 4. Excluded count

**1** (staff/admin WordPress role — 3 distinct staff accounts found in
source, most contributing only one lookup row each).

Also: **407** redundant duplicate rows (SKIP — same email and identity
as an already-included row, e.g. a repeat guest checkout; not an error,
needs no action).

## 5. Import method recommendation

**Admin GraphQL API** (not yet approved). Full evaluation:
`docs/PHASE10_CUSTOMER_STRATEGY.md` § 1. Decisive factor: Shopify's
customer CSV import has no metafield column and cannot carry the
proposed idempotency key at all.

## 6. Idempotency strategy

Match by `custom.legacy_woo_customer_id` metafield once established,
falling back to email pre-write; deterministic CREATE/UPDATE/QUARANTINE
outcome for every case. Full detail: `docs/PHASE10_CUSTOMER_STRATEGY.md` § 3.

## 7. Account/password strategy

This store runs **New Customer Accounts** (verified live). No password
field exists anywhere in the Shopify customer schema (verified via
schema introspection) — WooCommerce password migration is confirmed
technically impossible, not a risk to manage. No account
activation/invitation email is required by the platform. Full detail:
`docs/PHASE10_ACCOUNT_STRATEGY.md`.

## 8. GDPR/marketing-consent strategy

A real signal exists (FluentCRM): 6,295 subscribed, 229 unsubscribed, 21
pending, 5,551 unknown, of 12,096. **Default position: consent omitted
for every customer** (Shopify's own `NOT_SUBSCRIBED` default) until the
store owner explicitly confirms FluentCRM's original opt-in is a legally
sufficient basis to carry forward. Full detail:
`docs/PHASE10_GDPR_CONSENT.md`, ADR-014.

## 9. Test-import set

10 customers, selected from real data (not fabricated), covering
billing/shipping combinations, phone presence, all real consent states,
registered/guest, and both quarantine categories:
`reports/phase10_test_import_set.csv`.

## 10. Shopify development store

`wholesale-beautyhub.myshopify.com` — the same, and only, store used
throughout this project. No other store is in scope.

## 11. Required API scopes

`read_customers`, `write_customers`. **Currently NOT granted** — verified
live, a read-only customer query returns `ACCESS_DENIED`. Tracked as
issue #43, a technical prerequisite independent of the business
decisions below.

## 12. Rollback strategy

No mass deletion as a default recovery mechanism. Created-customer
ledger (WooCommerce ID → Shopify GID → timestamp → batch ID),
append-only execution log, and reconciliation state enable targeted,
human-reviewed remediation instead. Full detail:
`docs/PHASE10_CUSTOMER_STRATEGY.md` § 6.

## 13. Reconciliation strategy

Same pattern as Phase 9: fresh, independent Shopify queries after every
batch, field-by-field MATCH/MISMATCH/UNRESOLVED/NOT_APPLICABLE, never
rounded to "passed" with unresolved mismatches. Template:
`reports/phase10_customer_reconciliation_template.csv` (all rows
`NOT_APPLICABLE` — no write has occurred to reconcile yet).

## 14. Known risks

- Risk #39: no customer API scope granted (technical blocker)
- Risk #40: shipping addresses captured but unmapped (1,209 customers)
- Risk #41: real consent signal exists, not yet business/legally cleared
- Phone uniqueness collisions: untested against live Shopify (0 customers
  exist there yet)
- 63%/61% of customers missing phone/billing address (non-blocking —
  Shopify requires neither)

## 15. Outstanding business decisions (ADR-014, none resolved)

1. Import method approval
2. Marketing-consent policy (does FluentCRM's opt-in carry forward?)
3. Shipping-address mapping (add it, or accept the current billing-only gap?)
4. Test-import authorization (the 10-customer set above)
5. Bulk-import authorization (separate from all of the above)

---

## STATUS: BLOCKED

Not "ready for import" — **ready for human approval**, and blocked on
one additional technical prerequisite (§ 11) that isn't itself a
business decision. Per this project's governance: "looks good,"
"continue," or a technical PASS on this document do not constitute
approval. Exact statements needed are in issue #44 (method + test
import) and issue #19 (consent policy).
