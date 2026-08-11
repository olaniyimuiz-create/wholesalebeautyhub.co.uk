# Phase 10 — Customer Import Readiness Assessment

**Read-only assessment. No customer was imported, queried against
Shopify, or written anywhere by this document.**

## 1. Customer source data

- **Exists**: yes — `migration/data/customers.json` (12,096 records) and
  a pre-built Shopify-format import CSV,
  `shopify-theme/assets/shopify_customers_import.csv` (12,096 rows +
  header, count matches exactly).
- **Count**: **12,096** total — **6,649 registered**, **5,447 guest**
  (`is_registered` flag, sourced from `wp_wc_customer_lookup`).
- **Staff/admin exclusion**: already implemented in
  `migration/scripts/database_parser.py`'s `build_customers()` —
  excludes any account whose WordPress capabilities include
  `administrator`/`shop_manager`/`editor` and not `customer`
  (`STAFF_ROLES` filter). Verified this audit: 0 remaining accounts with
  a site-domain (`wholesalebeautyhub.co.uk`) email.
- **Duplicate emails**: **0** — all 12,096 emails are distinct.
- **Missing emails**: **0**.
- **Missing billing address (`address1`)**: 7,367 of 12,096 (61%).
- **Missing country code**: 5,434 of 12,096 (45%).
- **Privacy**: both source files confirmed `.gitignore`d
  (`git check-ignore` passes for both) — real customer PII has never
  been committed to the repository.

## 2. Shopify customer schema mapping

Already built and matches Shopify's CSV customer-import column format
exactly (First Name, Last Name, Email, Accepts Email Marketing, Default
Address fields, Phone, Accepts SMS Marketing, Tax Exempt, Tags). Guest
vs. registered is preserved via a `imported-from-woocommerce,guest` /
`,registered` tag, not a schema field guess.

## 3. Import method

**Not decided.** Phase 9's ADR-011 explicitly scoped the Admin API
decision to *products only* — it does not extend to customers. A
ready-to-use CSV already exists (§ 2), but whether Phase 10 uses that CSV
path, the Admin API (for parity with the product approach's idempotency
and audit trail), or a hybrid has never been formally chosen.

## 4. Idempotency strategy

**Not designed.** Phase 9's product idempotency (`custom.legacy_woo_id`
metafield, matched via a full-store scan before every write) has no
customer-side equivalent yet. Before any write, Phase 10 needs an
analogous mechanism — e.g. a customer metafield/note carrying the source
WooCommerce customer ID, or an email-based match strategy with an
explicit collision policy — so a Phase 10 test-then-bulk pattern (mirroring
Phase 9's) doesn't risk duplicate customer records.

## 5. Test customer import plan

**Not defined.** Phase 9 never wrote directly to the bulk scope — it ran
a 9-product controlled test first, reviewed real reconciliation results,
then sought separate authorization for the remaining 598. No equivalent
test-subset selection, reconciliation-report schema, or controlled-batch
plan exists yet for customers.

## 6. Privacy / security / consent

Marketing-consent handling (`Accepts Email Marketing` field on the
prepared CSV currently defaults to `no` for every record, per a spot
check) is an **explicitly open, already-tracked decision** — issue #19
("Decide long-term marketing-consent handling"). Whether WooCommerce's
original consent basis (if any) is legally sufficient to carry forward
into Shopify's marketing system, or whether every customer needs to be
re-permissioned, is a business/legal decision, not a technical one, and
not this pipeline's to make.

## 7. Approval requirements

**None exist.** No comment, ADR, or authorization of any kind for a
customer import — bulk or test — appears anywhere in this repository or
its GitHub issues. Issue #18 ("Import customers") has minimal acceptance
criteria (record count matches, spot-check a sample) but no method
decision, no authorization, and is explicitly independent of Phase 9.

## 8. Blocker classification

| Item | Classification |
|---|---|
| Customer import method (CSV vs. Admin API vs. hybrid) not chosen | **BUSINESS DECISION** |
| No idempotency strategy designed for customers | **TECHNICAL BLOCKER** |
| No test-customer-import plan/subset defined | **TECHNICAL BLOCKER** (planning) |
| Marketing-consent/GDPR handling undecided (issue #19) | **BUSINESS DECISION** |
| No explicit authorization for any customer write, test or bulk | **APPROVAL REQUIRED** |
| 61%/45% of customers missing address/country data | **NON-BLOCKING RISK** — Shopify does not require a customer to have an address; affects post-import data completeness, not import feasibility |

## 9. Conclusion

**Phase 9 closed; Phase 10 is BLOCKED** — not by missing source data
(12,096 clean, deduplicated, staff-excluded, schema-mapped records
already exist), but by three unresolved prerequisites: **(1)** no
customer import method has been chosen, **(2)** no idempotency strategy
has been designed, and **(3)** no authorization of any kind — test or
bulk — has been requested or granted. None of these is something this
pipeline can decide or assume. Per the store owner's own governance
pattern for Phase 9, the correct next step is the same shape: a formal
readiness/decision request (method, consent policy, test-batch plan)
posted for explicit approval — not an automatic start.

**Phase 10 was not started. No customer data was written, queried
against Shopify, or otherwise touched by this assessment.**
