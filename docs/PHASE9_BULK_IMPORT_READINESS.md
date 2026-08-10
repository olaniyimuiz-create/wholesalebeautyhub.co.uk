# Phase 9 — Bulk Product Import Readiness Checklist

Purpose: prepare the project for a **separate, explicit** bulk-import
approval covering the remaining 602 products. This document does not
grant that approval and nothing in it authorizes performing the import.
Companion to `docs/PHASE9_5_BULK_IMPORT_READINESS_REPORT.md` (narrative
summary) and `docs/DECISIONS.md` ADR-011 (the only import authorization
that exists so far — scoped to exactly 9 test products, not this).

## REPOSITORY

- [x] Clean — verified via `git status` at the start of this task
- [x] `origin/main` synchronized — verified via `git fetch` + `git log origin/main -1`

## SHOPIFY

- [x] Correct test store verified — `wholesale-beautyhub.myshopify.com`, confirmed live via `myshopifyDomain` on every preflight run this project has done
- [x] API authentication verified — real, live, re-verified on every import/reconcile run
- [x] Required scopes verified — all 9 (`read/write_products`, `read_product_listings`, `read/write_inventory`, `read/write_metaobjects`, `read/write_files`)
- [x] Environment confirmed non-production — `SHOPIFY_ENVIRONMENT=development` in `.env`, domain matches the one and only approved test store (ADR-011 Decision 3)

## IMPORTER

All tested against the live dev store with independent (non-importer-report) reconciliation, per `docs/PHASE9_INVENTORY_FIX_REPORT.md` and `docs/PHASE9_POST_TEST_REVIEW.md`:

- [x] Product import tested — 8/9 created, 1 correctly quarantined
- [x] Variant import tested — including the multi-option case (product 17, 9 variants) and the post-fix case (product 1721, 2 variants)
- [x] Pricing tested — regular/sale/compare-at, verified live
- [x] SKU tested — verified live
- [x] Vendor handling tested — importer sends the source value verbatim (verified by code inspection + live reconciliation); the one open discrepancy is Shopify's own behavior on a blank value, not an importer defect (§ Business Decisions)
- [x] Tags tested — verified live
- [x] Status tested — active/draft, verified live (product 333)
- [x] Media tested — verified live, including the AVIF-skip path
- [x] Inventory tested — **0 mismatches** after the risk #35 fix, verified via a 9-scenario test suite (`migration/scripts/test_phase9_inventory.py`) plus live reconciliation
- [x] Idempotency tested — re-run twice after the inventory fix alone; 0 duplicate products/variants/media each time, independently re-queried
- [x] Duplicate prevention tested — `custom.legacy_woo_id` matching verified across 4+ separate re-runs this project has performed

**No importer change is proposed by this document.** Per this task's
explicit instruction, the importer is not refactored without evidence —
none of the above surfaced a new defect requiring a code change.

## COLLECTIONS

- [x] Architecture approved — Phase 6.5, `shopify/foundation/collections.json` (`category_collections`: 26, `brand_collections`: 127, `manual_promo_collections`: 3, `excluded`: 2) and `docs/SHOPIFY_FOUNDATION.md` § Collection architecture (which separately states ~23 existing + 5 promoted + 127 + 3 ≈ 158 — the two documents count category collections slightly differently; not reconciled here, since it doesn't block this checklist, but flagged so nobody silently picks one over the other later)
- [ ] Collections approved for creation — **not yet.** No approval for collection creation exists anywhere in this repository. Issue #12 remains open
- [ ] Collections created in test store — **not performed this session**, per explicit instruction not to create collections without approval
- [ ] Handles verified — partially: re-ran `seo_url_mapper.py` fresh this session (not reused from Phase 5) and confirmed exactly 1 known handle collision (`/collections/valentine-combo-deals`, category vs. brand term) — already resolved at the *collection-planning* level by ADR-007 (both consolidated into one manual promo collection, not two colliding ones), but not yet re-verified against the live-created collections themselves, since none exist yet
- [ ] Product assignments tested — not performed; no collections exist to assign to

**Real, additional blocker found this session, not previously flagged
this precisely**: issue #12's own acceptance criteria list "Apply
data-quality fixes and regenerate products.json" (issue #10) as a
dependency. Issue #10 itself is still open — specifically because issue
#39 (apply the approved category-cleanup mapping) hasn't been done. This
means collection creation isn't just gated on an approval decision; it's
gated on a real, already-documented, not-yet-done implementation task
(§ Outstanding Decisions).

## MEDIA

- [ ] AVIF decision recorded — **pending** (14 files, issue #6). Proven safe to defer (test import skipped one cleanly, product 333), not yet decided whether to convert before or after bulk import
- [ ] Zero-image decision recorded — **pending** (4 products, issue #8). Proven safe to import without images (test import did so cleanly, product 70), not yet decided whether to source images first
- [x] Duplicate candidates disposition recorded — 12 candidates, classified **D (handle after import)** in `docs/PHASE9_POST_TEST_REVIEW.md` § 6; worst case is a redundant upload, not data corruption — does not block bulk import

## BUSINESS DECISIONS

- [ ] Product 2371 decision — **pending.** Option A (provide the real vendor) or Option B (explicitly approve importing with no vendor) — neither chosen. Product remains quarantined
- [ ] Blank-vendor policy decision — **pending.** Option A (accept Shopify's shop-name fallback) / B (implement an explicit alternative) / C (keep affected ~20 products quarantined until manually classified) — neither chosen
- [ ] Collection creation approval — **pending** (see COLLECTIONS above)
- [ ] Media decisions — **pending** (AVIF timing, zero-image sourcing — see MEDIA above)

## BULK IMPORT

- [x] Human approval explicitly requested — posted as a comment on GitHub issue #14, listing all 6 outstanding decisions (2026-08-10)
- [ ] Human approval explicitly recorded — **not recorded.** Nothing in this repository authorizes importing the remaining 602 products. ADR-011 Decision 5 explicitly scoped its authorization to the 9-product test only
