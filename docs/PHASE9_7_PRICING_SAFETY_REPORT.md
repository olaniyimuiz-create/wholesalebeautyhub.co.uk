# Phase 9.7 — Pricing Safety Report

Covers two real defects discovered mid-execution during the controlled
bulk import (Phase 9.7 Step 5), both fixed and both verified live before
the import continued. Neither was invented or guessed — each was found by
independent reconciliation against real Shopify data, root-caused against
raw source data, and its handling was an explicit store-owner decision
(ADR-012, ADR-013), not something this pipeline decided on its own.

## 1. Discovery — broken WooCommerce variations (ADR-012)

The pilot batch (first 15 of the approved 598) crashed on product 18 with
`IndexError: list index out of range` inside variant creation.

**Root cause**: raw `wp_postmeta` for WooCommerce variation post 10966
(child of product 18) shows a single row: `attribute_pa_shade = ''` — a
real, live, `publish`-status variation with a genuinely empty attribute
value. Not a parsing bug; the source data itself is incomplete. A
full-611-product scan found exactly one other case: variation 19990
(product 16464).

**Decision (ADR-012, Option A)**: skip only the broken variation; import
the parent product with its remaining valid variations. Never invent the
missing attribute value; never quarantine the whole product for one
broken variation among many valid ones.

**Result**: product 18 imports with 9 of 10 real variants; product 16464
with 22 of 23. Both reconcile with 0 mismatches. Every skip is recorded in
`reports/phase9_skipped_variations.jsonl` with classification
`BROKEN_VARIATION_SKIPPED`.

## 2. Discovery — fabricated £0.00 prices (ADR-013)

After a later tiered batch, independent reconciliation found product 69
("Msmetics 14In1 Lash Set") had 4 of its 11 variants live in Shopify at a
price of £0.00.

**Root cause**: `set_variable_variants`/`set_simple_variant` in
`migration/scripts/phase9_test_import.py` contained
`'price': str(price) if price else '0.00'` — whenever a variation's
`regular_price` and `price` were both empty in the WooCommerce source,
the importer silently substituted `'0.00'`. Product 69's 4 affected
variations (Phoenix, Enchanted, Dynamite, Unfeigned) genuinely have no
price data at all in WooCommerce. Notably, product 69 is already one of
the site owner's own pre-migration `PRICE_INTEGRITY_FLAGGED_IDS` (risk
#24) — this confirms the flag was pointing at a real problem, not a false
positive.

**Scope, checked across the full 611-product catalog** (not assumed):
- Product 69: 4 of 11 variants unpriced (partial)
- 10 wholly unpriced simple products, all `draft` status, none reachable
  by a shopper even if imported: 25089, 25092, 25109, 25111, 25113,
  25115, 25117, 25217, 25219, 25369

**Decision (ADR-013)**:
1. Product 69 — Option A (same precedent as ADR-012): import the parent
   with its 7 genuinely priced variants; skip the 4 unpriced ones
   (`MISSING_PRICE_SKIPPED` / `NO_SOURCE_PRICE`).
2. The 10 wholly unpriced products — quarantine entirely
   (`no_source_price`): nothing sellable to create, not imported.
3. Live correction of the 4 already-created £0.00 variants on product 69.

## 3. Code changes

- `migration/scripts/phase9_dry_run.py`: added `is_valid_variation()`,
  `partition_variations()`, `has_price()`, `partition_by_price()`,
  `flag_missing_price()`. Placed here (not in `phase9_test_import.py`) to
  avoid a circular import, since `phase9_test_import.py` already depends
  on this module.
- `migration/scripts/phase9_test_import.py`: `set_variable_variants()`
  now filters variations through both option-validity and price-validity
  before building any Shopify mutation payload, and returns two separate
  skip lists (`skipped_option`, `skipped_price`) so the audit trail names
  the real cause per variation. `set_simple_variant()` no longer has a
  `'0.00'` fallback — it raises `RuntimeError` if ever called with no
  price (should be unreachable given upstream quarantine; fails loud
  rather than silently fabricating). `expected_variant_count()`,
  `sync_inventory_for_existing()`, and `phase9_test_reconcile.py`'s
  `expected_variants()` all updated to only expect priced, option-valid
  variations — otherwise reconciliation would incorrectly validate a
  stray fabricated variant as "expected."
- `migration/scripts/phase9_bulk_import.py`: `QUARANTINE_CODES` now
  includes `no_source_price`; the manifest-count invariant check was
  generalized from a hardcoded "598/606" figure to "IMPORT + QUARANTINE +
  ALREADY_IMPORTED + EXCLUDE == 611" so it stays correct regardless of how
  many quarantine reasons exist.
- `migration/scripts/phase9_final_import_manifest.py`: added
  `variation_count_missing_price` / `missing_price_variation_ids` columns;
  `no_source_price` added to `QUARANTINE_CODES`.
- `migration/scripts/test_phase9_pricing.py` (new): 8 scenarios / 13
  assertions, all pure/local (no Shopify credentials needed) — valid
  regular price, valid sale+regular price (existing compare-at logic
  unaffected), empty price, 7-priced/4-unpriced partial product, 0-priced
  variable product (quarantine), 0-priced simple product (quarantine),
  the `'0.00'` fabrication guard (raises, doesn't fabricate), and stray
  £0.00 variants not being treated as valid expected data during
  reconciliation. All pass. Re-ran the existing
  `test_phase9_inventory.py` suite (9 live scenarios) — no regression.

## 4. Live correction of product 69

Schema-introspected before use (read-only): confirmed
`productVariantsBulkDelete(productId: ID!, variantsIds: [ID!]!)` is the
correct, minimal, currently-supported mutation for removing specific
variants without touching the parent product or other variants.

Located product 69 via the authoritative full-store scan
(`fetch_existing_legacy_map`) — not Shopify's metafield search-query
syntax, which this session caught returning a **different, wrong**
product GID for the same query, corroborating this project's long-standing
distrust of that syntax.

Deleted exactly the 4 fabricated-price variant IDs. `userErrors: []`.
Independently re-queried: product 69 now has exactly 7 variants, all with
their real source prices (£25–£35), title/status/vendor/media untouched,
parent product intact.

## 5. Regression / dry-run verification

Full 611-product dry-run re-run after the code changes: `0` blocking
issues, `611/519/497` products/published/variants unchanged from before —
no regression. A fresh full-catalog scan (not reused from any prior
report) confirms the missing-price scope is exactly: 10 products
`no_source_price`, 1 product (`69`) `partial_missing_price` — nothing
else, across all 611 products including the already-quarantined and
already-imported ones.

## 6. Live reconciliation and idempotency

- **Product 69, post-correction**: independently reconciled — 7/7
  variants match source exactly (title, price, SKU, inventory). Re-ran
  the importer against product 69 again: 0 created, 1 updated, 0
  variants recreated — still exactly 7 variants, no £0.00 anywhere.
- **Full batch reconciliation** (all products processed this session,
  588 total): 9,143 field-level comparisons, 106 mismatches — all 106
  fall into exactly the two pre-existing, non-blocking, already-documented
  categories (17 vendor shop-name-fallback per risk #34/issue #41, 89 tag
  comma-splitting, a genuine Shopify `tags` field behavior — commas
  delimit tags, confirmed via live testing, no data lost, just split more
  granularly). **Zero pricing mismatches remain. Zero new defect
  categories were found in any batch after the fix.**
- **Idempotency — definitive, full-store**: a direct Shopify query (not a
  re-run of the importer, for the authoritative final check) confirms 596
  products in the store, 596 distinct `legacy_woo_id` metafield values,
  596 distinct product GIDs — an exact 1:1:1 mapping. Zero duplicate
  products, zero duplicate legacy IDs, zero orphaned/unmatched products.

## 7. Final state

| Metric | Value |
|---|---|
| Fabricated £0.00 prices remaining | **0** |
| Legitimate (source-backed) £0.00 prices anywhere in the catalog | **0** |
| Products imported this session (598 approved − 10 reclassified) | 588 |
| Products already imported (all-time total) | 596 |
| Products quarantined (`ambiguous_vendor`) | 5 |
| Products quarantined (`no_source_price`) | 10 |
| Products excluded | 0 |
| Total catalog | 611 |
| Duplicate products/variants/media | 0 |

Not claimed complete merely because the scripts ran without error —
every number above is independently re-verified against live Shopify
data, not the importer's own self-report.
