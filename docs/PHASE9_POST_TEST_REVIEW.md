# Phase 9 — Post-Test Review

Formal review of the first controlled Shopify test import (ADR-011),
covering what was found, what was fixed, what remains open, and exactly
what's required before the remaining 602-product catalog can be imported.
This is not authorization for that import — see § 12.

## 1. Test summary

9 products from `reports/phase9_test_import_set.csv` written to the
approved dev store (`wholesale-beautyhub.myshopify.com`) via
`migration/scripts/phase9_test_import.py`, across four runs this session:

1. **Initial import**: 8 created, 1 quarantined (product 2371), 0 hard
   failures. Live reconciliation found 3 real mismatches (§ 2).
2. **Source-data fix + idempotency check**: fixed the root cause of one
   mismatch (§ 4), re-ran — 0 created (correctly matched all 8 by
   `legacy_woo_id`), 8 updated, product 1721's variants retried and fixed.
3. **Inventory-quantity fix attempt**: found and fixed a second real
   defect (untracked/unset inventory, § 7) but the fix itself failed
   against the live API for a different real reason (§ 2, mismatch 3) —
   left honestly failing rather than papered over.
4. **Final idempotency re-verification**: re-ran again — still 0 created,
   8 updated, 1 quarantined, no duplicate products/variants/media.

## 2. Reconciliation findings — all 3 original mismatches

Source: `reports/phase9_test_import_reconciliation.csv` (live Shopify
queries, independent of the importer's own report).

| # | Product / field | WooCommerce source | Shopify (initial run) | Root cause | Classification | Resolution |
|---|---|---|---|---|---|---|
| 1 | 1721, `variant_count` | 2 variants | 1 (default only) | `productOptionsCreate` rejected: "Must specify an option name" | **Real migration defect** (parser bug, not Shopify or source-data behavior) | **Resolved** — see § 4 |
| 2 | 1721, `variant_price_sku` | priced, SKU'd | not set | Same root cause as #1 | Real migration defect | **Resolved** — see § 4 |
| 3 | 7535, `vendor` | `''` (blank) | `'Wholesale Beautyhub '` | Shopify auto-fills the shop name as Vendor when `productCreate` receives an empty `vendor` string | **Intentional Shopify platform transformation**, not a defect in this project's code | **Explicitly accepted as open** — needs a human business decision (issue #41), not a code change. Confirmed affects 20 of 611 products (all blank-vendor products, mostly ADR-007 bundles), not just this one |

No mismatch was hidden, suppressed, or reclassified to manufacture a
PASS. Mismatch 3 remains a mismatch in the final reconciliation, on
purpose — it is unresolved, not swept aside.

### New mismatches surfaced by fixing the above

Fixing mismatch 1/2 required adding inventory-quantity handling to the
importer (§ 7), which in turn surfaced a **fourth, previously-invisible
real defect**: Shopify's `inventorySetQuantities` mutation rejects every
call on this API version with "The @idempotent directive is required for
this mutation but was not provided" — a real, current API requirement not
implemented here. Classification: **real migration defect, unresolved**.
13 inventory-quantity field comparisons now correctly show as mismatches
(e.g. product 17's "Dubai-04" variant: source `2`, live `0`) — reported
honestly, not hidden. Full detail: `docs/RISK_REGISTER.md` #35.

**Final reconciliation state**: 161 field-level comparisons, 146 matched,
15 mismatches (1 vendor auto-fill × 1 product, pending a business
decision; 13 inventory-quantity gaps across 6 products, pending an API
fix; 1 residual — see notes column in the CSV for exact rows).

## 3. Product 2371 — vendor ambiguity

**Investigated exhaustively, not just re-read from the existing flag.**
Checked, in order of increasing effort:

1. `vendor` field: `'Accessories _make_up'` (slug `unnamed`) — the known
   placeholder pattern (risk #14/#31).
2. Product title/category pattern across all 5 products sharing this
   vendor (1726, 2369, 2370, **2371**, 2372): all 5 are generic equipment
   —a foldable makeup chair, LED ring lights, a tripod, an office pin —
   not branded cosmetics. Consistent with white-label/dropship accessory
   items that were never assigned a real manufacturer brand in
   WooCommerce, not with a data-entry mistake that could be reverse-engineered.
3. Product description (`body_html`): empty string. No brand mention.
4. **Full raw `wp_postmeta` scan for product 2371 directly against
   `dump.sql`** (257 rows, independent of `products.json`'s parsed
   fields): no meta key resembling manufacturer/brand/supplier anywhere;
   the 257 rows are overwhelmingly empty theme-builder scaffolding fields.

**Decision: insufficient evidence to safely classify. Remains
quarantined.** Guessing a vendor from a product title (e.g. inferring
"generic" or a retailer name) is exactly the kind of silent
business-classification decision this project's governance prohibits.

**Affected Shopify fields if imported as-is**: `vendor` would be blank
(triggering the same shop-name auto-fill as mismatch 3 above) unless a
real value is supplied.

**Proposed human decision** (not made here): either (a) the store owner
identifies the real supplier/brand for these 5 products from records
outside this database, or (b) an explicit decision to import them with no
Vendor value (accepting the auto-fill, or setting an explicit "Generic"/
placeholder value). Once decided, this becomes a single deterministic
rule applied to `database_parser.py`'s `BRAND_NAME_CORRECTIONS`/
`EXCLUDED_BRAND_NAMES` pattern (already proven for the Topicrem/Valentine
cases), not a code change to *this* review.

**Can it import after manual approval?** Yes — nothing about product
2371 is otherwise broken (title, price, images, category all resolve
cleanly per `reports/phase9_product_data_quality.csv`). Vendor is the
only blocker.

## 4. Product 1721 — blank variation attribute name

**Root cause found in the raw source data, not guessed.** The real
serialized `_product_attributes` WordPress meta for product 1721:

```
a:1:{s:0:"";a:6:{s:4:"name";s:1:"%";s:5:"value";s:9:"20% | 10%"; ...}}
```

The array **key** is an empty string (`s:0:""`), but the **name** field —
the actual human-entered attribute label — is the literal character
`"%"`. This is consistent with the option values (`20%`, `10%`): the
merchant named a percentage-based attribute just `%`. WooCommerce
normally derives the array key via `sanitize_title($name)`; for a name
made entirely of the character `%` (no alphanumerics), that sanitization
produces an empty string — real, explainable WooCommerce behavior, not
corrupted data.

`database_parser.py`'s `attribute_label()` derived the option name from
the array **key**, which is correct for `pa_*` taxonomy attributes (key
IS the taxonomy slug) and happens to also work for 9 other custom
attributes in this catalog (their sanitized keys title-case cleanly back
to "Size"/"Colour"/etc.) — but breaks for this one case where the key
lost all information.

**Fix applied** (`migration/scripts/database_parser.py`):
```python
attribute_label(key, attribute_taxonomies) or (info.get('name') or '').strip() or 'Option'
```
Only falls back to the real `name` field when the key-derived label is
empty — does not invent anything, and does not touch any other product's
behavior.

**Verified before trusting it**:
- Scanned all 10 catalog products with a custom (non-taxonomy) variation
  attribute where key-derived label differs from the raw `name` field.
  9 of 10 are unaffected (key-derived label — e.g. "Size" from "SIZE" —
  is non-empty, so the fallback never triggers; their existing,
  already-correct title-cased labels are unchanged). Only product 1721's
  key was empty.
- Regenerated `products.json`, diffed: only product 1721 changed
  (`variation_option_names` went from `['']` to `['%']`).
- Re-ran `database_parser.py` twice, byte-identical output — fix is
  idempotent.
- Re-ran the full `phase9_dry_run.py` pipeline: identical summary numbers
  to before the fix (611/519/497/0 blocking/151/31/4/9) — no regression.
- Re-ran the test import: product 1721's `productOptionsCreate` now
  succeeds with option name `%` and both variants (20%, 10%) get their
  real price/SKU set. Confirmed via **live reconciliation**, not the
  importer's own claim.

**Decision: resolved.** No attribute name was invented — the real one
(`%`) was read from a field the original code wasn't reading. Not
converted into a different product structure (still 1 option, 2 variants,
exactly matching the source).

## 5. Collections — not ready, not authorized this session

**Architecture exists and is approved** (Phase 6.5): 156–158 planned
collections (26 category-driven incl. 5 promoted, 127 brand-driven, 3
manual promo) in `shopify/foundation/collections.json` and
`docs/SHOPIFY_FOUNDATION.md` § Collection architecture. The dry-run's
`resolve_collections()`/`build_collection_lookup()` logic (reused, not
re-derived, by the test import for the *intended* collections it records
per product) already resolves 151 of the catalog's real category/brand
references against this file.

**Not created in Shopify.** The live store has 0 real collections (the 1
found during pre-flight is Shopify's own default, not one of ours).
GitHub issue #12 ("Create collections via Admin API") remains open.

**Known staleness risk, already documented before this review**:
`collections.json`'s own header explicitly says "re-verify counts before
Phase 9 - the catalog changes," and risk #32 (open, unchanged) notes the
approved Phase 6.5 category-cleanup mapping was never mechanically
applied to `database_parser.py` — ~8 stray categories still won't resolve
to a planned collection as-is.

**No conflicts found** in URL/handle mapping — the one real historical
collision (Valentine/Tradefair brand-vs-category) was already resolved via
ADR-007 in Phase 6, and the Phase 5 redirect matrix was cross-checked
against it at the time with no open conflicts remaining.

**Decision**: Collection creation is **out of scope for this review and
not authorized** — ADR-011 authorized exactly the 9-product test import,
not collection creation (a separate, not-yet-approved scope, issue #12).
The collection creation plan is: (1) resolve risk #32 (apply the cleanup
mapping mechanically), (2) re-verify `collections.json`'s counts against
current `products.json` per its own stated caveat, (3) build a collection
Admin API creation script reusing `resolve_collections()`/
`build_collection_lookup()`, (4) get explicit approval, (5) run it against
the test store first, same pattern as this product test. **Stopping here
per this step's explicit instruction.**

## 6. Media dependencies

Source: `docs/MEDIA_MIGRATION.md`. Classified A (must fix before bulk
import) / B (safe to skip) / C (needs human approval) / D (handle after
import):

| Item | Count | Classification | Why |
|---|---:|---|---|
| AVIF assets (incl. featured-image AVIF cases) | 14 | **B** | Proven safe in the test import itself (product 333): the product imports correctly with that one image simply excluded, not with a broken/missing-file error. Real conversion (WebP/JPEG) is recommended for completeness but does not block a product from importing correctly |
| Missing/broken assets | 9 | **B** | Already excluded from `images` at the source-parsing stage (never in the list to begin with) — nothing for the importer to trip over |
| Duplicate candidates (metadata-fingerprint matches, not confirmed-identical) | 12 | **D** | Worst case is a redundant upload, not data corruption or a failed import; a binary-hash/visual check is real work but doesn't block correctness |
| Non-image files (3 HTML + 1 CSV in the media library) | 4 | **B** | Confirmed via `media_manifest.json`: all 4 have `usage_type='unused'` and no `product_id`/`variation_id` — not referenced by any product, irrelevant to product import entirely |
| Zero-image products | 4 | **C** | Technically safe to import (proven: product 70 imported correctly with 0 images) but a real content gap a merchant needs to either accept or fix by sourcing images — not this pipeline's call to make silently |
| Filename/alt-text strategy | — | **Ready** | Already fully decided (`docs/MEDIA_MIGRATION.md` §14–15) and consumed as-is by the media manifest the importer already reads from; no outstanding decision |

No destructive media operation was proposed or performed.

## 7. Importer changes

Reviewed `migration/scripts/phase9_test_import.py` against the full
checklist. Only made changes where the test's own evidence demonstrated a
genuine defect — nothing rewritten speculatively:

| Area | Status | Change made |
|---|---|---|
| Idempotency key (`custom.legacy_woo_id`) | Working, verified | None |
| Duplicate detection | Working, verified (full-store scan by metafield) | None |
| Quarantine handling | Working, verified (product 2371) | None |
| Pricing / compare-at pricing | Working, verified via live reconciliation | None |
| SKU handling | Working, verified | None |
| Vendor handling | Working as sent; Shopify's own auto-fill is the open item, not this code | None (§ 2, § 3) |
| Tags, product status | Working, verified | None |
| Media handling | Working, verified (AVIF correctly excluded) | None |
| **Variant/option creation** | **Real defect**: update path unconditionally skipped variant setup even when a prior run left it incomplete | **Fixed**: update path now checks live variant count vs. expected, and only retries variant setup when incomplete — never touches an already-complete product's variants, so this can't create duplicates |
| **Inventory handling** | **Real defect**: `tracked` was set unconditionally on any SKU'd variant regardless of WooCommerce's `manage_stock`, and no quantity was ever set at all | **Partially fixed**: `tracked` now correctly gated on `manage_stock == 'yes'` (matching the existing, already-documented CSV-generator convention). Quantity-setting was attempted but the underlying Shopify mutation itself fails on this API version (§ 2, § 6) — left honestly failing and logged, not silently dropped or faked |
| Collection assignment | Deliberately not implemented (§ 5) | None — correctly out of scope |
| Logging | Working; one real gap found and fixed (silently swallowed inventory errors) | **Fixed**: `set_inventory_quantities` now checks its own response and logs `INVENTORY_SET_FAILED` with the real error, instead of the caller ignoring the return value entirely |
| Checkpointing | Working, verified across all 4 runs this session | None |
| Retry logic (rate limits/5xx) | Not implemented | Not exercised by this test (no rate-limit/5xx ever occurred at this scale) — no evidence to act on, left as a documented gap rather than built speculatively |
| Rollback behavior | Not implemented as code (design documented in `docs/PHASE9_ENVIRONMENT_READINESS.md`) | Not exercised (nothing needed rolling back) — left as-is |

`migration/scripts/phase9_test_reconcile.py` also updated: added
inventory-quantity comparison (the gap that led to discovering risk #35
in the first place) and fixed a self-inflicted false-mismatch in how
quarantined-product rows were compared (cosmetic, not a data issue).

## 8. Validation results

- Ran `migration/scripts/phase9_dry_run.py` after the `database_parser.py`
  fix: identical summary to the pre-fix baseline (611 products, 519
  published, 497 variants, 0 blocking issues, 151 collections resolved,
  31 unmapped, 4 zero-image, 9 AVIF-featured) — confirms the fix didn't
  regress anything else in the catalog.
- Ran the corrected `phase9_test_import.py` against the same 9-product
  set: 0 created, 8 updated, 1 quarantined, 0 hard failures.
- Ran `phase9_test_reconcile.py` (fresh, independent Shopify queries):
  161 comparisons, 146 matched, 15 mismatches — all explained in § 2,
  none hidden.

## 9. Idempotency results

**PASS**, re-verified after every code change this session (not just
once): product count held at 8 across all re-runs (never grew), product
17's variant count held at 9 (never doubled to 18), product 1721's
variant count correctly went from 1 (broken) to 2 (fixed) exactly once
and stayed at 2 on the final re-run, not duplicated. Product 2371 stayed
quarantined on every run — never accidentally created.

## 10. Remaining risks

From `docs/RISK_REGISTER.md`, real and open as of this review:

- **#31** (open, unchanged): product 2371 and 4 siblings — ambiguous
  vendor, needs a human decision (§ 3).
- **#32** (open, unchanged): approved category-cleanup mapping not
  mechanically applied — blocks full collection accuracy (§ 5).
- **#34** (open): Shopify's shop-name vendor auto-fill for 20 blank-vendor
  products — needs a business decision (§ 2, § 3).
- **#35** (new, open): `inventorySetQuantities` requires an `@idempotent`
  directive/header not yet implemented — blocks real inventory-quantity
  accuracy for the full catalog (§ 2, § 7).
- **#33**: resolved this review (§ 4).
- Pre-existing, still open, not touched by this review: #2 (CSV format
  drift risk), #4 (order history), #13 (tag casing), #16 (AVIF
  conversion not yet executed), #21 (`shopify theme dev` still untested,
  unrelated to product import).

## 11. Remaining human decisions

1. Product 2371 (and siblings 1726, 2369, 2370, 2372): real vendor, or
   explicit accept-as-blank decision (§ 3).
2. Blank-vendor shop-name auto-fill: accept for all 20 affected products,
   or set an explicit placeholder value (§ 2, § 3).
3. Collection creation: separate approval needed (issue #12), after
   resolving risk #32 (§ 5).
4. Zero-image products (4 of 611): accept as-is or source images first
   (§ 6).
5. AVIF conversion (14 files): execute the conversion before full import,
   or accept the skip-and-backfill-later approach proven safe in testing
   (§ 6).
6. Inventory-quantity fix (risk #35): needs real engineering work
   (implementing `@idempotent` + idempotency-key generation) and a fresh
   live re-test before it can be trusted for the full catalog (§ 2, § 7).

## 12. Exact approval required for bulk import

**Not requested by this review.** Per ADR-011 Decision 5 and this
project's absolute safety rule, a successful/improved test import does
not imply authorization for the remaining 602 products. Before that
separate approval can be meaningfully requested, at minimum:

- Decisions 1–2 above resolved (or explicitly, formally deferred with a
  documented fallback rule).
- Risk #35 (inventory quantities) fixed and **re-verified live**, not
  assumed fixed from this review's diagnosis alone.
- Collection creation (§ 5) either completed and verified, or an explicit
  decision that the full product import may proceed before collections
  exist (accepting products with no collection membership temporarily).
- A fresh test-import + reconciliation + idempotency cycle after the
  above, since this review's fixes themselves haven't been proven at
  anything beyond this same 9-product scale.

Until then: **BLOCKED for bulk import**, same as before this review —
what changed is *what* it's blocked on, not *whether* it's blocked.
