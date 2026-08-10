# Phase 9 — Inventory Fix Report (Risk #35)

Closes the technical blocker identified in `docs/PHASE9_POST_TEST_REVIEW.md`:
all 8 test-imported products had `tracked: true` variants but
`inventoryQuantity: 0` regardless of the real WooCommerce stock level,
because Shopify's `inventorySetQuantities` mutation was rejecting every
call. This document traces the defect end to end, the real (not guessed)
Shopify API requirements that caused it, the fix, and independent live
verification.

## 1. End-to-end trace

| Stage | Status before this fix | Status after |
|---|---|---|
| WooCommerce source (`stock_quantity`, `manage_stock`) | Correct — verified directly against `dump.sql` in the prior review | Unchanged, still correct |
| `products.json` (`database_parser.py`) | Correct — `stock_quantity`/`manage_stock` parsed accurately at product and variation level | Unchanged |
| Transformed import payload (`phase9_test_import.py`) | Correct — `expected_inventory()` computed the right target quantity, gated on `manage_stock == 'yes'` | Unchanged |
| Inventory item identification | Correct — `inventoryItem.id` captured from the variant create/update response | Unchanged |
| Location identification | Correct — `get_default_location()` resolves the store's real location, verified live | Unchanged |
| **`inventorySetQuantities` mutation call** | **Broken — rejected by Shopify, two real schema requirements missing** | **Fixed — see § 3, § 4** |
| Shopify response handling | Broken — failure was silently swallowed (return value never checked) | Fixed alongside the mutation itself |
| Independent reconciliation | Correctly detected the gap (13 mismatches) — reconciliation logic itself was never the problem | Now confirms 0 inventory mismatches |

**The defect was isolated to exactly one stage**: the mutation call itself
(wrong shape) and the caller's failure to check its result. Every stage
before and after was already correct — re-verified, not assumed, by
tracing each one individually.

## 2. Shopify API requirements — verified against real documentation

Fetched `shopify.dev` directly (not from memory) for
`inventorySetQuantities` and the idempotent-requests guide, then
**independently confirmed every claim against the live store** rather
than trusting the fetched summary alone (one fetched detail —
`ignoreCompareQuantity` — turned out not to exist on this schema; the
live API response is the ground truth used here, not the doc summary):

1. **`@idempotent(key: $key)` directive is mandatory.** Shopify made
   idempotency mandatory for inventory-adjustment and refund mutations as
   of API version 2026-04
   ([changelog](https://shopify.dev/changelog/making-idempotency-mandatory-for-inventory-adjustments-and-refund-mutations)).
   Syntax is a GraphQL directive on the mutation field itself, not an
   HTTP header and not an `input` field:
   ```graphql
   inventorySetQuantities(input: $input) @idempotent(key: $key)
   ```
2. **`changeFromQuantity` is required per `InventoryQuantityInput` entry**
   — confirmed by testing directly against the live store (the schema
   rejected a call missing it with `"InventoryQuantityInput must include
   the following argument: changeFromQuantity."`). This is the quantity
   the caller currently believes is set; Shopify uses it as a
   compare-and-set guard.
3. **`ignoreCompareQuantity` is not a real field** on this schema — an
   earlier attempt included it based on an assumption, and the live API
   rejected it outright (`"Field is not defined on
   InventorySetQuantitiesInput"`). Removed.

Sources: [inventorySetQuantities](https://shopify.dev/docs/api/admin-graphql/latest/mutations/inventorySetQuantities), [Idempotent requests](https://shopify.dev/docs/api/usage/idempotent-requests), [Making idempotency mandatory](https://shopify.dev/changelog/making-idempotency-mandatory-for-inventory-adjustments-and-refund-mutations).

## 3. Minimum safe fix implemented

`migration/scripts/phase9_test_import.py`:

- Added `fetch_current_available_quantities()` — one batched `nodes(ids:
  [...])` query per call (not N+1 queries) to get the real current
  quantity for every inventory item about to be set.
- `set_inventory_quantities()` now sends `changeFromQuantity` per entry
  and wraps the mutation with `@idempotent(key: $key)`, using a fresh
  `uuid.uuid4()` per call — a fresh key per call is correct here: it
  protects one request from being double-applied if retried after a
  network failure, not meant to be reused across this script's separate
  runs. What makes *re-running the whole script* safe is the mutation's
  absolute-set semantics plus always re-fetching the real current value
  first, not key reuse.
- **Nothing else was touched.** `custom.legacy_woo_id` idempotency,
  product/variant/media duplicate detection, checkpointing, structured
  logging, quarantine handling, and the variant-retry-on-incomplete logic
  from the prior review are all unchanged — verified by diff, not just by
  intent.

Two related real bugs, found by the test suite in § 5 (not invented
speculatively), were fixed in the same pass since they're in the exact
code path being verified:
- `fetch_current_available_quantities` crashed (`AttributeError`) on a
  nonexistent inventory item, since a missing `nodes` entry comes back as
  `null`, not absent. Now treated as quantity 0, not raised.
- The same function crashed (`KeyError: 'data'`) on a malformed/error-only
  API response (no `data` key at all). Now returns an empty result and
  lets the caller's existing error handling take over, instead of
  crashing before that error handling is ever reached.

## 4. Inventory mutation — before/after

**Before:**
```graphql
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    userErrors { field message }
  }
}
```
Every call rejected: `"The @idempotent directive is required for this
mutation but was not provided."` The caller never checked the response,
so this failure was silently swallowed — every affected product still
reported `INVENTORY_SYNCED` in the log despite nothing being set.

**After:**
```graphql
mutation($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { changes { name delta } }
    userErrors { field message }
  }
}
```
with `changeFromQuantity` populated per entry from a fresh live query, and
the response's `userErrors`/top-level `errors` checked and logged
(`INVENTORY_SET_FAILED` with the real error) rather than assumed
successful.

## 5. Test coverage

`migration/scripts/test_phase9_inventory.py` — run, not just written; all
9 scenarios pass for real (`python migration/scripts/test_phase9_inventory.py`,
exit code 0):

| # | Scenario | Kind | Result |
|---|---|---|---|
| 1 | Positive quantity | LIVE | Set to 7, live query confirms 7 |
| 2 | Zero quantity | LIVE | Set to 0, live query confirms 0 |
| 3 | Multiple variants | LIVE | Product 17's 9 real variants, all set correctly in one call |
| 4 | Multiple inventory items | LIVE | Same call as #3 — 9 inventory items in one batched mutation |
| 5 | Re-running the same update | LIVE | First call → 5, second call → still 5 (not 10) |
| 6 | API validation failure | LIVE | Invalid quantity `name` correctly rejected: `"The quantity name must be either 'available' or 'on_hand'."` (first attempt assumed negative quantity would fail — it doesn't, Shopify allows it; test corrected rather than left silently wrong) |
| 7 | Missing inventory item | LIVE | Nonexistent inventory item ID handled without crashing (found and fixed the real bug in § 3) |
| 8 | Missing location | LIVE | Invalid location ID correctly rejected: `"The specified location could not be found."` |
| 9 | Partial/malformed API response | MOCK | Response missing `data` entirely handled as failure, not crashed (found and fixed the real bug in § 3) |
| 10 | Retry behaviour | MOCK | Documents the real, current gap honestly: no retry logic exists. A transient-error response results in exactly 2 calls (fetch current + attempt set), not a 3rd retry call — confirms the absence of retry logic rather than fabricating one |

LIVE tests run against `wholesale-beautyhub.myshopify.com` using product
124's and product 17's real inventory items (already-existing test data,
no new product created). `tearDownClass` resets scratch values used
during testing; the subsequent full production re-run (§ 7) overwrites
everything with the real source `stock_quantity` values regardless.

**Note on the committed audit log**: `reports/phase9_test_import_log.jsonl`
contains 4 entries from this test suite's mocked failure scenarios
(`"Throttled"`, `"upstream timeout"` — scenarios 9/10, synthetic by
design) interleaved with real import entries. They're self-evidently
distinguishable — mocked entries have no `woo_id` field, which every real
product log entry always has — not edited out, since editing a log file's
history would itself misrepresent what happened.

## 6. Live test store verification (pre-write)

Re-ran the existing 11-point pre-write preflight before writing anything
this session — all passed: authentication, correct store identity
(`wholesale-beautyhub.myshopify.com`), all 9 required scopes, test CSV
integrity, not-production check, import method, idempotency key. No
change was needed here; the existing preflight already covers this.

## 7. Independent reconciliation — inventory specifically

`migration/scripts/phase9_test_reconcile.py`, fresh live Shopify queries,
run after the fix and after the full production re-import:

**Before this fix**: 148 comparisons, 134 matched, 13 inventory-quantity
mismatches (e.g. product 17 "Dubai-04": source `2`, Shopify `0`) + 1
vendor mismatch (§ 8).

**After this fix**: **161 comparisons, 160 matched, 1 mismatch** (the
vendor auto-fill, § 8 — unrelated to inventory, already known, explicitly
not a defect). **Zero unexplained inventory discrepancies.** Every tested
inventory quantity matches its WooCommerce source exactly — no rounding,
no exclusions, no "close enough": e.g. product 17's 9 variants
`[2, 0, 0, 0, 1, 2, 0, 0, 0]` match the source `stock_quantity` values
exactly, in order (Dubai-04, Cape Town-05, Doha-02, Ethiopia-08,
Florida-01, Ghana-09, Kenya-10, Mauritius-03, Nigeria-07).

## 8. Product 7535 — vendor investigation (Step 10)

**Root cause confirmed, not re-guessed:**

- Source data (`products.json`): `vendor = ''` — correct and intentional.
  This product's only brand-shaped category term is "VALENTINE COMBO
  DEALS," which ADR-007 explicitly excludes from the Vendor system (it's
  a mislabeled seasonal promo, not a real brand) — so a blank vendor here
  is the *correct* transformation of the source data, not a defect.
- Importer code (`create_product`/`update_product`, lines 171/198):
  `'vendor': product['vendor']` — sent verbatim, no fallback, no
  injected default. Confirmed by direct code inspection this session,
  unchanged by any fix in this report.
- Shopify's own behavior: given `vendor: ""` on `productCreate`, Shopify
  auto-fills the shop's display name ("Wholesale Beautyhub") as the
  product's Vendor, rather than leaving it genuinely blank.

**Classification: C — Shopify platform behavior.** Not A (no fallback was
ever designed), not B (the importer passes the source value through
unmodified), and the source data itself (D) is correct, not the cause of
the mismatch — the *source* is right; what Shopify *does* with a
correctly-blank vendor is the actual cause.

**No vendor was invented or changed.** This remains open, exactly as
instructed — it's a business decision (accept the shop-name default for
all 20 affected products, or set an explicit placeholder), not a code fix.

## 9. Full 9-product re-test (Step 8) and idempotency (Step 9)

Ran the complete test import twice more after the fix (once to apply it
for real, once purely to verify idempotency):

| | Run (fix applied) | Run (idempotency check) |
|---|---|---|
| Products attempted | 9 | 9 |
| Created | 0 (all 8 already existed — matched by `legacy_woo_id`) | 0 |
| Updated | 8 | 8 |
| Quarantined | 1 (product 2371 — unchanged) | 1 |
| Failed | 0 | 0 |
| Product count (live, independently queried) | 8 → 8 | 8 → 8 |
| Product 17 variant count (independently queried) | 9 (unchanged) | 9 (unchanged) |
| Product 1721 (prior fix from post-test review) | Still correct — 2 variants, both priced/SKU'd | Still correct |
| Product 2371 | Still quarantined, not created | Still quarantined |

**No duplicate product, variant, or media item was created at any point.**
Every check in this section was performed via a fresh, independent
Shopify API query — never inferred from the importer's own log alone.

## 10. Collections and media — unchanged

Per Step 11/12's explicit instruction, neither was touched this session.
Status remains exactly as `docs/PHASE9_POST_TEST_REVIEW.md` left it:

- **Collections**: architecture approved (Phase 6.5), not created in the
  store, issue #12 remains separate and unauthorized for this task.
- **Media**: 14 AVIF (safe to skip/defer), 4 zero-image products (safe to
  skip/defer pending approval), 12 duplicate candidates (post-import
  handling) — classification unchanged, no new evidence this session
  affects it.

## 11. Validation gate

- [x] Inventory mutation corrected (§ 3, § 4)
- [x] Inventory write succeeds against the live test store (§ 5, § 7)
- [x] Inventory quantities independently reconciled (§ 7 — 0 mismatches)
- [x] No unexplained inventory mismatches
- [x] Product 1721 remains correct (§ 9)
- [x] Product 2371 remains safely quarantined (§ 9) — no human approval has been recorded, so it stays quarantined
- [x] Product 7535 is documented (§ 8) — classification C, left open, not fixed
- [x] Full 9-product test passes (§ 9)
- [x] Idempotency passes (§ 9)
- [x] No duplicate resources created (§ 9)
- [x] Repository validation passes (dry-run pipeline re-run, no regressions — see commit)
- [x] Working tree clean, changes committed and pushed (see commit hash below)

## 12. What this report does not authorize

Per this task's explicit instruction: a PASS here means the importer is
technically ready for the **next human approval gate** — it does not mean
the remaining 602 products, any customer, or any order are authorized.
Bulk import was not requested, performed, or implied by this fix.
