# Phase 9.7 — Final Human Approval Gate

Purpose: reverify everything technical, consolidate every remaining
decision into one approval matrix, define the exact bulk-import scope,
and formally request authorization. **Nothing in this document is that
authorization.** No product was imported, no collection was created, and
no business decision was made on the store owner's behalf while
producing it.

## 1. Reverification (Step 1)

All performed fresh this session, live against the real store, not
assumed from prior reports:

| Check | Result |
|---|---|
| Repository | Clean, `origin/main` fetched and matches, commit `254422e` at task start |
| Shopify store identity | `wholesale-beautyhub.myshopify.com`, shop "Wholesale Beautyhub", plan "Grow App Development" |
| Authentication | PASS (live query) |
| Required API scopes | PASS — all 9 granted |
| Environment | `development` — confirmed not production |
| Current Shopify product count | **8** (live query, `productsCount`) |
| Existing imported test products | Verified individually by `legacy_woo_id`: 124, 17, 7535, 1687, 70, 22, 1721, 333 — matches documented history exactly, nothing missing or duplicated |
| Current Shopify collection count | 1 (Shopify's own default — not one created by this project) |
| Importer/script state | Commit `254422e` — unchanged since Phase 9.6, no new evidence required a change |
| Category mapping status | PASS — issue #39 resolved (Phase 9.6), re-confirmed via a fresh `phase9_dry_run.py` run this session: 611/519/497, 0 blocking, 158 collections resolved, 24 unmapped (unchanged) |
| SEO redirect mapping status | PASS — unchanged since Phase 9.6, not re-run this session since no source data changed |
| Inventory implementation | PASS — unchanged since the risk #35 fix, 0 live mismatches last verified |
| Idempotency implementation | PASS — unchanged, `custom.legacy_woo_id` matching verified across 5+ separate re-runs this project has performed |

No safety check failed. Proceeding to Steps 2–9 only — not to import.

## 2. Approval matrix (Step 2)

| # | Decision | Current status | Options | Consequence of each | Recommended | Blocks bulk import? |
|---|---|---|---|---|---|---|
| 1 | Product 2371 vendor | **PENDING** — no approval anywhere in this repo (issue #38) | A: provide the real vendor. B: explicitly approve blank vendor | A: imports normally with a real brand. B: imports with Shopify's shop-name fallback (same as item 2) | Not chosen here — no option is favored over the other by this document | **Yes**, for this product and its 4 siblings only (598 others unaffected) |
| 2 | Blank-vendor policy (~20 products, incl. product 7535) | **PENDING** (issue #41) | A: accept Shopify's shop-name fallback. B: override with an approved alternative value. C: leave genuinely blank (see § technical note below). D: quarantine affected products | A: simplest, ships with "Wholesale Beautyhub" as brand on ~20 products. B: needs the exact replacement value specified. C: **not confirmed technically achievable** — see below. D: same treatment as product 2371, ~4x the volume | Not chosen here | **Yes**, for the ~20 affected products only |
| 3 | Collection creation | **PENDING** (issue #12) — architecture approved, technical blocker (#39) now resolved | Approve creation now / defer | Approve: ~156–158 collections created in the **test store only**, verified live before any product assignment. Defer: products import without collection membership until approved later | Not chosen here | No — collections and products are separate Shopify objects; products can be created without collection membership and assigned later. Does not block bulk import, but affects what shoppers can browse in the interim |
| 4 | AVIF handling (14 files) | **PENDING** (issue #6) | Convert now / defer | Convert: all 14 products get real images before import. Defer: those 14 products import without their AVIF-format image (proven safe — product 333 already did this cleanly); no other image is invented as a substitute | Not chosen here | No — proven safe to defer in the live test |
| 5 | Zero-image products (4) | **PENDING** (issue #8) | Source images / import without | Source: delays those 4 specifically. Import without: proven safe (product 70) | Not chosen here | No — proven safe to defer in the live test |
| 6 | Bulk import authorization | **PENDING** — requested on issue #14 (Phase 9.5), not yet approved | Approve / decline / defer | Approve: this document's manifest (§ 7) becomes eligible for a controlled, batched import into the **test store only**. Decline/defer: no change | Not chosen here | **Yes** — the actual gate this entire document exists to request |

**No option above was chosen by this document.** Where prior documentation
already recorded an approval (ADR-011, ADR-007, ADR-009, the category
cleanup implementation), it's cited in § 1 and not re-requested — none of
the 6 items above have that status; all remain genuinely open.

### Technical note on blank-vendor Option C

Investigated via a safe, read-only GraphQL schema introspection this
session (`__type(name: "ProductInput") { inputFields }`) — no mutation
was performed to test this further. The schema's own field description
for `vendor` is just `"The name of the product's vendor"` — a plain
nullable string; it documents no distinction between "explicitly blank"
and "never set," and no flag to suppress the shop-name display fallback.
Combined with the already-tested live behavior (an empty string produces
the shop-name fallback, confirmed on product 7535), there is no evidence
in this project that Option C is achievable through the Admin API as
currently understood. Presented honestly as **not confirmed technically
possible**, not silently dropped from the list.

## 3. Collections (Step 3)

Confirmed before considering any creation:

- **Approved collection mapping**: `shopify/foundation/collections.json`
  (156 planned) + Phase 9.6's category cleanup (issue #39) — current,
  re-verified this session.
- **No unresolved category mapping would cause products to disappear**:
  the only categories that don't resolve to a collection are (a) the 4
  Level-1 nav-only groups (by design — they're navigation structure, not
  collections, per ADR-009) and (b) 24 Level-3 product-type categories
  (also by design — they become Shopify Product Type, not a collection)
  plus "Uncategorized" (4 products, still needs manual audit, tracked
  separately, not silently dropped).
- **Handles verified**: re-confirmed via Phase 9.6's fresh
  `seo_url_mapper.py` run — 0 duplicate old URLs.
- **Duplicate-handle collisions are intentional consolidation**: 4 found,
  all 4 individually confirmed as multiple old category/brand URLs
  correctly redirecting to one real collection (3 from the category
  cleanup, 1 the pre-existing Valentine/Tradefair case) — not errors.

**No approval for collection creation exists in this repository.**

**COLLECTION CREATION: AWAITING HUMAN APPROVAL.**

Nothing was created. Stopping this part here, per explicit instruction.

## 4. Product 2371 (Step 4)

No new evidence sought or found — already exhaustive (title, description,
category, and a full raw 257-row `wp_postmeta` scan, per
`docs/PHASE9_POST_TEST_REVIEW.md` § 3). **Remains quarantined.** Excluded
from the bulk-import manifest's `IMPORT` set (§ 7) — classified
`QUARANTINE`, along with its 4 siblings (1726, 2369, 2370, 2372), which
share the identical unresolved vendor issue and have never been
individually re-investigated since none has ever produced different
evidence. No vendor fabricated. No shop-name value silently assigned.

## 5. Blank-vendor policy (Step 5)

No decision recorded anywhere. Marked **PENDING** in § 2 above with all
four options (A/B/C/D) presented, including the technical finding on
Option C. Once an option is approved, it should be encoded in
`migration/scripts/phase9_test_import.py` as a named, documented policy
(e.g. a `VENDOR_FALLBACK_POLICY` constant with an explicit source
citation to the ADR/issue that approved it) — **not implemented as a
guess in this document**, since no approval exists yet to encode.

## 6. Media policy (Step 6)

No decision recorded anywhere. Both AVIF timing and zero-image sourcing
remain **PENDING** in § 2. No destructive media conversion was performed
or proposed. No image was invented for any product.

## 7. Final import manifest (Step 7)

Generated fresh this session:
`migration/scripts/phase9_final_import_manifest.py` →
`reports/phase9_final_import_manifest.csv` — one row per all 611
catalog products, with WooCommerce ID, Shopify legacy ID, title, product
type, vendor, SKU, price, compare-at price, inventory
(`manage_stock`/`stock_quantity`), status, resolved collections, tags,
variant count, image count, quarantine reason where applicable, and
`import_eligibility`.

**Real, previously-imprecise figure corrected here**: earlier phases
consistently stated "602 remaining products" (611 − 9, where 9 = the
original test set of 8 created + 1 quarantined). Generating the *full*
manifest for the first time shows this undercounted the quarantine
class: **5 products** share product 2371's exact ambiguous-vendor issue
(1726, 2369, 2370, 2371, 2372), not just the 1 that happened to be in the
original test sample. The accurate breakdown of the 603 products not yet
in the store:

| Classification | Count | Meaning |
|---|---:|---|
| `ALREADY_IMPORTED` | 8 | The original test set — unchanged, verified live (§ 1) |
| `IMPORT` | **598** | Clean, no blocking data-quality issue, no ambiguous vendor |
| `QUARANTINE` | **5** | The ambiguous-vendor group — excluded until § 2 item 1 is decided |
| `EXCLUDE` | 0 | No product currently has a BLOCKING data-quality issue |

**598 + 5 + 8 = 611.** Nothing classified `QUARANTINE` or `EXCLUDE` is
eligible for import under this manifest.

## 8. Final pre-import reconciliation (Step 8)

Re-ran the full dry-run pipeline fresh this session (not reused):
611 products, 519 published, 497 variants, **0 blocking data-quality
issues**, 158 collections resolved, 24 categories genuinely unmapped (by
design — Level-3 product types + the still-open "Uncategorized" case),
4 zero-image products, 9 AVIF-featured products. Every number here
traces to `reports/phase9_dry_run_summary.csv` and
`reports/phase9_final_import_manifest.csv`, generated by the same run
this document reports.

**Zero unexplained blocking discrepancies.** The only products excluded
from `IMPORT` status (the 5 `QUARANTINE` products) are excluded for a
fully documented, already-known reason — not a new or unexplained one.

## 9. Human authorization gate (Step 9)

**STOP.** Per explicit instruction, bulk import is not executed. The
formal authorization request — containing test store, import method,
approved scope, quarantine treatment, vendor policy, media policy,
collection policy, and the non-production/no-customers/no-orders
acknowledgements — is posted as a GitHub comment on issue #14 (see the
session's commit/PR trail for the exact link). It is not fabricated or
assumed satisfied by "looks good," "continue," or any prior technical
PASS.
