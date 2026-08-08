# Phase 9: Product Import — Mapping, Methodology, and Dry-Run Results

Analysis, transformation, and validation only — **no Shopify store was
contacted**, no product was created, updated, or deleted anywhere. Every
number in this document comes from `migration/scripts/phase9_dry_run.py`
actually running against a freshly-regenerated `migration/data/products.json`,
re-verified for idempotency (two full runs, byte-identical output).

## Architecture

```
migration/sql/dump.sql
  → database_parser.py (fixed this phase, see § Pipeline fixes)
  → migration/data/products.json
  → phase9_dry_run.py ─┬─ reads shopify/foundation/collections.json (Phase 6.5)
                        ├─ reads migration/data/media_manifest.json (Phase 8)
                        ├─ queries wp_termmeta directly for shade/color hex (new)
                        └─ writes reports/phase9_*.csv (7 reports)
```

No new database parsing logic was built — `phase9_dry_run.py` imports
`sql_utils.py` (the same streaming parser every prior script uses) only
for the one piece of data no existing JSON output carries yet: shade/color
hex values and their per-variation attribute assignment.

## Pipeline fixes applied this phase

Three real, previously-known, already-approved issues were fixed at the
source (not worked around in the dry run) — all verified against real
data before and after:

| Issue | Fix | Verification |
|---|---|---|
| Risk #9 / GitHub #11 — Product Type used first category, not most specific | `database_parser.py` now computes true category depth via the real `product_cat` parent chain (`most_specific_category_for()`) | Product 17: categories `['Face','Foundation']` → `product_type` now `'Foundation'`, not `'Face'` |
| ADR-007 (approved) — Valentine/Tradefair mistagged as brands | `database_parser.py` excludes `EXCLUDED_BRAND_NAMES` from Vendor extraction | Confirmed: neither name appears in any product's `vendor` field after regeneration |
| Risk #15 — Topicrem/Tropicrem/Topicream brand spelling | `database_parser.py` normalizes via `BRAND_NAME_CORRECTIONS` | Investigated fully: only 2 real products involved (not 4 as originally estimated) — each is tagged with **both** the correct and a misspelled brand term simultaneously; `brands[0]` already resolved to the correct spelling by luck of ordering, the correction now makes it deterministic rather than coincidental |

One new bug found and fixed by this phase's own dry run (not previously known):

| Issue | Fix | Verification |
|---|---|---|
| 12 draft products had an **empty Shopify handle** (`post_name` blank — normal WordPress behavior for drafts never explicitly published) — would have silently merged into one product on import | `database_parser.py` `unique_handle()`: falls back to a slugified title when `post_name` is empty, with a numeric-suffix collision guard | Before: 12 products sharing handle `''`. After: 611 products, 611 distinct handles, 0 empty |

A second dry-run-only bug (not a source-data problem) was also found and
fixed: the collection-name lookup didn't decode HTML entities
(`Bath &amp; Body Care` vs. `Bath & Body Care`) or normalize case
(`acne treatment` vs. `Acne Treatment`), producing 14 false "unmapped"
results. Fixed via `normalize_name()` (decode + casefold on both sides).

## Field-by-field mapping

| WooCommerce | Shopify | Notes |
|---|---|---|
| Product ID | `custom.legacy_woo_id` metafield | Not the Shopify product ID (assigned on create) — this is the deterministic join key for idempotent re-imports |
| Variation ID | (no separate metafield defined) | Variant identity in Shopify is derived from its option values under the parent product; the parent's `legacy_woo_id` plus option values is sufficient to re-identify a variant deterministically |
| SKU | Variant SKU | Direct copy; 267 products have no SKU at all — not blocking (Shopify allows blank SKU), flagged LOW |
| `_regular_price` | Variant Compare At Price | Only set when it differs from the resolved price (avoids a fake strikethrough) |
| `_price` (falls back to `_regular_price`) | Variant Price | |
| `_sale_price` | (not directly mapped) | WooCommerce's sale price is already reflected in `_price`; Shopify's own Price/Compare-At pair captures the same "was/now" relationship without a third field |
| Product title | Title | Direct copy |
| `post_name` (slug) | Handle | Direct copy, with the empty-handle fallback above |
| `post_content` | Body (HTML) | Direct copy |
| `post_excerpt` (short description) | *(not separately mapped)* | This catalog's parser doesn't currently carry excerpt as a distinct field from the CSV/JSON pipeline; body content is the single description source. Flagged as a documentation gap, not a data-loss bug — no Phase 3/4 output ever separated the two |
| `product_cat` (most specific) | Product Type | Fixed this phase, see above |
| `product_cat` (Level 2) | Collection membership | Resolved via `shopify/foundation/collections.json`'s `category_collections` |
| `pwb-brand` | Vendor **and** Collection membership | Per ADR-006 hybrid: Vendor field always set; Collection resolved via `collections.json`'s `brand_collections` |
| `product_tag` | Tags | Direct copy, comma-joined with categories (matches `csv_generator.py`'s existing behavior) |
| Variation attributes (`attribute_pa_*`) | Option name/value | Already handled by the existing pipeline (`database_parser.py`'s `variations[].options`) |
| Shade/color attribute value | **Proposed**: metafield or native swatch, hex from `wp_termmeta.rey_attribute_color` | **Not approved architecture yet** — see § Colour/shade mapping below |
| Product images (`_thumbnail_id`, `_product_image_gallery`) | Product media | Resolved via `migration/data/media_manifest.json` (Phase 8) — not re-derived |
| Variation image (`_thumbnail_id` on variation) | Variant media relationship | Same source |
| SEO title | *(no per-product override found in source)* | Confirmed in Phase 6.5: 0 of 611 products have custom Rank Math SEO meta — only 9 *pages* do (out of Phase 9's scope). Shopify SEO title defaults to product title, which is the correct behavior here, not a gap |
| SEO description | *(no per-product override found in source)* | Same as above — defaults to a truncated product description |
| WooCommerce ID | `custom.legacy_woo_id` metafield | Already defined in `shopify/foundation/metafields.json` (Phase 6.5); this phase is the first to actually populate it in dry-run output |

No mapping in this table was invented without a source — where WooCommerce
has no equivalent data (per-product SEO overrides, short description as a
distinct field), that's stated explicitly rather than papered over.

## Colour/shade mapping — proposed, not approved

Real investigation (not assumption): 326 variation-level shade/color
attribute assignments exist across this catalog.

| Status | Count | Meaning |
|---|---:|---|
| Real hex color available | 132 (40.5%) | The attribute term has a `wp_termmeta.rey_attribute_color` value |
| Term exists, no color set | 184 (56.4%) | The shade/color term is real but was never assigned a color in the source theme |
| Orphaned (references a nonexistent term) | 10 (3.1%) | The variation's attribute value doesn't match any real term slug — e.g. one product line uses plain numbers ("1", "2", ... "8") that were never turned into real `pa_shade` terms |

**Proposed mapping** (per GitHub issue #37, opened during Phase 8):
represent shade/color as a Shopify metafield on the variant (or product
option metafield, depending on how Shopify's native swatch configuration
is finally set up in Phase 7's theme — that decision belongs to whoever
builds the real swatch UI, not this document). `reports/phase9_variant_mapping.csv`
carries `shade_slug`, `shade_hex_proposed`, and `colour_mapping_status`
per variation so the real mapping work has a starting point.

**This is explicitly not committed to as final architecture** — per this
phase's governance, a proposal is documented and flagged for approval,
not silently implemented. The 10 orphaned references need a decision
(what should they actually be?) before any implementation, not a guess.

## Price integrity — issue #34 resolved with real findings

All 24 flagged product IDs were checked directly against
`migration/data/products.json`:

- **Confirmed for real**: all 24 have `price` set but `regular_price` and
  `sale_price` both empty. The site owner's concern was valid and remains
  present in the current data.
- **Broader pattern found**: this isn't unique to the 24 flagged IDs — **96
  of 611 products (15.7%)** have the same empty-`regular_price` pattern.
  The 24 were a sample the owner happened to investigate, not the full
  extent of the pattern.
- **Impact assessed, not just described**: an empty `regular_price` does
  **not** produce a broken Shopify import — `csv_generator.py`/the dry
  run's compare-at logic only sets Compare At Price when `regular_price`
  is present *and* differs from `price`; when it's empty, Compare At
  Price is correctly left blank rather than showing a fabricated "was"
  price. Flagged LOW severity (`empty_regular_price`), informational, not
  blocking.
- **10 products have no price at all** (not just empty regular_price) —
  all 10 are **draft** status, not published. Flagged MEDIUM
  (`missing_price`), not blocking, since draft products aren't part of
  the 519 published products this phase's reconciliation counts as
  importable.

Issue #34's acceptance criteria (spot-check the 24 IDs, correct or
exclude before import) is satisfied by this investigation: nothing needs
excluding, since the pattern doesn't produce a broken import — but the
finding is real and documented, not dismissed.

## Data-quality results (real, from `reports/phase9_product_data_quality.csv`)

382 issues found across 611 products, **0 BLOCKING** (after the pipeline
fixes above — there were 12 before the empty-handle fix):

| Severity | Count | Dominant cause |
|---|---:|---|
| LOW | 353 | `missing_sku` (267, informational — Shopify allows blank SKU), `empty_regular_price` (86, see above) |
| MEDIUM | 20 | `missing_price` (10, all drafts), `ambiguous_vendor` (5, see below), `zero_images` (4), `missing_category` (1) |
| HIGH | 9 | `avif_primary_image` — a product's *featured* image specifically is AVIF, higher urgency than an AVIF sitting unused in the gallery |

**5 products quarantined for human review, not auto-resolved** (risk
#14): vendor `'Accessories _make_up'` (product IDs 1726, 2369, 2370,
2371, 2372) is a data-entry placeholder, not a real brand. Per this
phase's stop conditions ("a product requires ambiguous business
classification"), this is not guessed at — each product needs a human to
determine its real vendor (or confirm it has none).

## Collection/brand resolution (real, from `reports/phase9_collection_mapping.csv`)

151 distinct categories/brands resolved to a real collection handle.
31 remain genuinely unmapped after excluding the 4 Level 1 groups
(Makeup, Skin Care, Bath & Body Care, Beauty Tools — which are
**correctly** not collections per ADR-009, they're navigation structure
only). The 31 are mostly Level 3 sub-categories (Foundation, Concealer,
Eyeliner, etc.) that ADR-009 also says should **not** become their own
collections — they're already captured correctly via the `product_type`
field (§ Pipeline fixes) instead. The genuinely actionable subset is
small: `Body Body`, `TRADEFAIR COMBO DEAL`, `Dark spots & Discoloration
serums`, `Glow serum`, `Glow spray`, `Eye cream`, `sponge`,
`Uncategorized` — these are exactly the stray top-level categories
`docs/SHOPIFY_FOUNDATION.md`'s approved cleanup mapping already covers,
but that mapping has not yet been mechanically applied to
`database_parser.py`. **Not fixed in this phase** — reclassifying ~8
categories correctly carries real risk of error under time pressure on
an already large change set; tracked as a new issue (§ below) rather than
rushed.

## Media resolution

Reused Phase 8's `media_manifest.json` directly — no re-derivation, no
re-inventory. `reports/phase9_media_mapping.csv` joins every product/
variation to its resolved media records, `target_filename`, and
`validation_status`. 9 products have an AVIF file as their *featured*
image specifically (of the 14 total AVIF files Phase 8 found) — these are
the highest-priority conversion targets, not the other 5 AVIF files
sitting unused/in gallery-only positions.

## Reconciliation (source vs. transformed payload — NOT vs. a live store)

**No Shopify store exists, so this cannot be a post-import reconciliation.**
It compares WooCommerce source counts to this dry run's transformed
payload counts — proof the transformation preserves the data, not proof
anything landed in Shopify.

| Category | Source | Transformed | Status |
|---|---:|---:|---|
| Products (all statuses) | 611 | 611 | PASS |
| Products (published) | 519 | 519 | PASS |
| Products importable (post-quarantine) | 519 | 519 | PASS — 0 blocking after fixes |
| Variants (all) | 497 | 497 | PASS |
| Variants (published products) | 455 | 455 | PASS |
| Distinct SKUs | 554 | 554 | PASS |

## What this phase did NOT do

- Did not create, update, or query any Shopify product, collection, or
  metafield definition — no Shopify credentials exist in this project.
- Did not resolve ADR-010 (Markets/B2B/plan tier) — still open.
- Did not decide CSV vs. Admin API execution — recommended (Admin API,
  `docs/PHASE9_IMPORT_STRATEGY.md`), not executed.
- Did not implement the colour/shade metafield — proposed only.
- Did not apply the full category-cleanup mapping — the 3 fixes in
  § Pipeline fixes were applied because they were unambiguous technical
  bugs or already-approved decisions; the remaining category renames are
  a real but separate task (new issue below).
- Did not touch the 5 ambiguous-vendor products' data.
- Did not run a test or production import of any kind.
