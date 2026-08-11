# Phase 10 — Customer Import Readiness Assessment

**Updated 2026-08-11. Read-only. No Shopify customer write of any kind
has occurred at any point in this project.**

## 1. Customer source data — independently re-verified from raw source

Recomputed directly from `wp_wc_customer_lookup`/`wp_usermeta` in
`migration/sql/dump.sql` (not from `migration/data/customers.json`) via
`migration/scripts/phase10_customer_dry_run.py`:

| Metric | Count |
|---|---:|
| Raw `wp_wc_customer_lookup` rows | 13,043 |
| **IMPORT-eligible** | **12,096** |
| QUARANTINE — missing email | 292 |
| QUARANTINE — conflicting identity sharing one email | 247 |
| SKIP — redundant duplicate row (same identity, repeat checkout) | 407 |
| EXCLUDE — staff/admin account | 1 (3 distinct staff accounts, most with only 1 lookup row) |
| Registered | 6,649 |
| Guest | 5,447 |
| Missing phone | 7,646 (63%) |
| Missing billing address | 7,367 (61%) |
| Has a real shipping address (currently unmapped — see risk #40) | 1,209 (10%) |

The 12,096 IMPORT figure, and the 6,649/5,447 registered/guest split,
match what `customers.json` already contained — independently confirmed,
not just inherited. What's new: **539 records that were previously
silently dropped by `build_customers()` with no audit trail** now have
an explicit, reviewable disposition (`reports/phase10_customer_quarantine.csv`).

Idempotency of the transformation itself: ran the dry run twice,
manifest and statistics output were byte-identical.

## 2. Customer field mapping

Complete: `docs/PHASE10_CUSTOMER_MAPPING.md` / `reports/phase10_customer_mapping.csv`.
One real gap found: shipping addresses are captured by the parser but
never surfaced (risk #40) — not yet decided whether to add them.

## 3. Customer account architecture — verified live

This store runs **New Customer Accounts** (`customerAccountsVersion:
NEW_CUSTOMER_ACCOUNTS`, verified via live query). `CustomerInput` has no
password field at all (verified via schema introspection) —
**WooCommerce password migration is confirmed technically impossible**,
not merely inadvisable to assume. No activation/invitation email is
required by the platform. Full detail: `docs/PHASE10_ACCOUNT_STRATEGY.md`.

## 4. GDPR / marketing consent — real signal found, not yet applied

A genuine consent signal exists via FluentCRM (`wp_fc_subscribers`,
previously never read by any script): 6,295 `subscribed`, 229
`unsubscribed`, 21 `pending`, 5,551 with no signal at all. Whether
FluentCRM's original opt-in is legally sufficient basis to carry into
Shopify is a business/legal decision, not decided here. Default position:
omit `emailMarketingConsent` for every customer until explicitly
approved. Full detail: `docs/PHASE10_GDPR_CONSENT.md`, ADR-014.

## 5. Import method — formal recommendation made, not approved

**Recommended: Admin GraphQL API**, evaluated independently for
customers (not copied from the product decision) — decisive factor is
that Shopify's customer CSV import has no metafield column, so it cannot
carry the proposed `custom.legacy_woo_customer_id` idempotency key at
all. Full evaluation: `docs/PHASE10_CUSTOMER_STRATEGY.md` § 1.

## 6. Idempotency strategy — designed, not yet exercised

Match by `custom.legacy_woo_customer_id` metafield once established,
falling back to email pre-write. Deterministic CREATE/UPDATE/QUARANTINE
outcomes for every case, including the "email changed on the Shopify
side after a prior import" case. Full detail:
`docs/PHASE10_CUSTOMER_STRATEGY.md` § 3.

## 7. Test customer import plan — designed, not executed

10-customer representative set built, covering billing/shipping address
combinations, phone presence, all 3 real consent states plus "unknown,"
registered/guest, and both quarantine categories:
`reports/phase10_test_import_set.csv`. **Not imported.**

## 8. A genuinely new technical blocker found this session

The app installation has **no `read_customers` or `write_customers`
scope at all** — verified live, a read-only customer query returned
`ACCESS_DENIED`. This must be resolved (scope expansion in Shopify
Admin) before even a read-only verification query becomes possible, let
alone a write. Tracked as risk #39.

## 9. Approval requirements

**None exist.** No comment, ADR, or authorization for any customer
write — test or bulk — appears anywhere in this repository or its
GitHub issues (checked fresh this session). ADR-014 records the six
decisions needed; none are answered yet.

## 10. Blocker classification

| Item | Classification |
|---|---|
| App has no `read_customers`/`write_customers` scope | **TECHNICAL BLOCKER** |
| Customer import method (Admin API recommended, not approved) | **APPROVAL REQUIRED** |
| Marketing-consent/GDPR policy (real signal exists, not applied) | **BUSINESS DECISION** (with a genuine legal question embedded) |
| Shipping address mapping gap | **BUSINESS DECISION** (scope choice, not urgent) |
| Test import authorization (10-customer set, designed) | **APPROVAL REQUIRED** |
| Bulk import authorization | **APPROVAL REQUIRED**, separate from the above |
| 63%/61% of customers missing phone/billing address | **NON-BLOCKING RISK** — Shopify requires neither |

## 11. Conclusion

**Phase 9 closed; Phase 10 technical preparation is now substantially
complete** — dataset independently re-verified with full audit trail,
field mapping documented, account architecture and consent options
verified live (not assumed), import method formally recommended,
idempotency and rollback designed, a representative test set built.
**Phase 10 remains BLOCKED**, now by concrete, named items rather than
"not yet designed": one real technical blocker (customer API scopes
never granted) and five approval/business-decision gates (ADR-014).
None of these is something this pipeline can resolve on its own.

**Phase 10 was not started. Zero Shopify customer writes occurred. No
customer was created, updated, or fetched by GID at any point.**
