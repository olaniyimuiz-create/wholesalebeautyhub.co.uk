# Phase 9.5 — Bulk Product Import Readiness & Approval

Purpose: prepare the project for a separate, explicit decision on
whether to import the remaining 602 products. **This document does not
make that decision and does not authorize it.** It exists to put every
outstanding technical fact and business choice in one place so that
decision can be made deliberately, not to move the project toward bulk
import on its own.

## PHASE 9.5 STATUS: **BLOCKED**

Not "partial" — every item under Business Decisions below is fully
unresolved (zero of four decided), and Collections has a real,
independent implementation dependency (issue #10/#39) on top of the
approval it's also missing. "Blocked" is the accurate word, not a
softer one chosen to look further along than it is.

**Technical importer:** PASS
**Inventory:** PASS
**Idempotency:** PASS
**Collections:** NOT READY
**Media:** DEFERRED (decisions pending)
**Product 2371:** PENDING
**Blank vendor policy:** PENDING

## 1. Technical readiness (importer, inventory, idempotency)

All three are genuinely PASS, carried forward from
`docs/PHASE9_POST_TEST_REVIEW.md` and `docs/PHASE9_INVENTORY_FIX_REPORT.md`
— re-verified this session by inspection, not re-run, since no new
evidence emerged requiring a re-test:

- Product/variant/pricing/SKU/tags/status/media: all verified live across
  multiple re-runs.
- Inventory: 0 mismatches after the risk #35 fix, verified via a
  9-scenario test suite and live reconciliation.
- Idempotency: re-run twice after the inventory fix alone, 0 duplicates
  each time, independently re-queried.

**No importer code change is proposed or was made this session.** Per
explicit instruction, the importer isn't touched without evidence of a
defect — none surfaced.

## 2. Product 2371 — decision required (not made)

Vendor `'Accessories _make_up'` (data-entry placeholder). Exhaustively
investigated across two prior reviews: title, description, category, and
a full raw 257-row `wp_postmeta` scan against `dump.sql` directly — no
real vendor signal exists anywhere in the source data.

**Present to the business owner, unchosen:**

- **Option A** — Provide the correct vendor (requires information from
  outside this database — supplier records, purchase invoices, etc.)
- **Option B** — Explicitly approve importing the product with no
  vendor (Shopify allows a blank Vendor field; it's a valid, supported
  state, not a broken one)

Product 2371 (and its 4 siblings — 1726, 2369, 2370, 2372, same vendor
value, tracked in issue #38) **stays quarantined until one of these two
is explicitly chosen.** Neither is chosen by this document.

## 3. Blank-vendor policy — decision required (not made)

**What's actually happening, stated precisely so it can't be misread as
either side's fault:**

- The WooCommerce source data for these ~20 products (mostly ADR-007
  reclassified bundle products, e.g. product 7535) has a **genuinely
  blank** vendor value — this is correct, intentional data, not missing
  or corrupted data.
- `migration/scripts/phase9_test_import.py` sends that value **verbatim**
  (`'vendor': product['vendor']`, confirmed by direct code inspection) —
  it does not substitute, guess, or default anything.
- **Shopify's own platform behavior**, on receiving an empty `vendor`
  string in `productCreate`/`productUpdate`, automatically displays the
  store's own shop name ("Wholesale Beautyhub") as that product's Vendor.
  This is not something WooCommerce ever said and not something this
  project's code invented — it is not being represented as sourced from
  WooCommerce anywhere in this repository.

**Present to the business owner, unchosen:**

- **Option A** — Accept Shopify's shop-name fallback for these ~20
  products (simplest; means they'll display "Wholesale Beautyhub" as
  their brand)
- **Option B** — Implement an approved alternative fallback (e.g. a
  neutral placeholder like "Generic" or the product's category as a
  stand-in) — needs the exact replacement value specified by the business
  owner, not invented here
- **Option C** — Keep the affected ~20 products quarantined until each is
  manually classified (same treatment as product 2371, at ~4x the volume)

**No option is selected.** The decision must be recorded in
`docs/DECISIONS.md` (as an ADR, following the ADR-011 pattern) or on
GitHub issue #41 before bulk import proceeds.

## 4. Collections — the major technical dependency

### 4.1 What's approved

Phase 6.5 approved a concrete collection architecture:
`shopify/foundation/collections.json` (machine-readable) and
`docs/SHOPIFY_FOUNDATION.md` § Collection architecture (human-readable):

| Type | Count (per `collections.json`) | Count (per `SHOPIFY_FOUNDATION.md` prose) |
|---|---:|---:|
| Category-driven | 26 | 23 existing + 5 promoted = 28 |
| Brand-driven | 127 | 127 |
| Manual promo | 3 | 3 |
| Excluded | 2 | — |
| **Total** | **156** | **~158** |

**The two documents don't agree on the category-collection count** (26 vs.
28) — not reconciled by this report, since it doesn't block anything
here, but flagged explicitly so it isn't silently picked one way later.
Whoever implements collection creation should resolve this from
`collections.json` directly (the machine-readable source of truth) rather
than the prose count.

### 4.2 The Valentine/promo case specifically

Per this task's explicit instruction to pay attention to it: "VALENTINE
COMBO DEALS" exists in the source data as **both** a `product_cat` term
*and* a `pwb-brand` term with the identical slug — a real handle collision
if both were naively created as separate collections
(`/collections/valentine-combo-deals` claimed twice). **Already resolved
at the architecture level** by ADR-007: the brand term is dropped from
the Vendor/brand-collection system entirely (it was a mislabeled seasonal
promo, not a real brand), and "Valentine Combo Deals" exists as exactly
**one** manual (curated) promo collection, alongside "Tradefair Combo
Deals" (same pattern — a near-duplicate brand term dropped, one manual
collection kept) and "Combo Deals" (general). Re-verified this session
via a fresh `seo_url_mapper.py` run: the raw category-vs-brand collision
still shows up in a naive URL scan (`reports/duplicate_urls.csv`, 1 row)
**because that scan doesn't know about ADR-007's consolidation** — it's
not a live problem, just confirms the *reason* ADR-007 exists is real and
still present in the raw source data.

### 4.3 SEO / legacy URL relationships

`reports/redirect_matrix.csv` already maps every category (`/collections/{handle}`)
and brand (`/collections/{handle}`) URL to its planned Shopify destination
— re-generated fresh this session (not reused from Phase 5), which also
caught and fixed 12 stale redirect-target URLs left over from before the
empty-handle fix (risk #30) — e.g. product 25371 now correctly redirects
to `/products/eos-body-lotion-toasted-marshmallow-2` (collision-guard
suffix, since another product already claims the base handle) instead of
a stale, broken `/shop//` target. This was a real, previously-uncaught
inaccuracy, now corrected and committed as part of this session's work
(not itself a new decision — the underlying handles were already fixed in
Phase 9, this just re-generated the report that reads them).

### 4.4 Handle-collision check (issue #12's own acceptance criterion)

Re-ran `seo_url_mapper.py` this session specifically because issue #12
requires it before collection creation: **0 duplicate old URLs, 1
colliding new URL** (the Valentine case, § 4.2 — already resolved at the
collection-planning level, not a live blocker).

### 4.5 Real, additional blocker found this session

Issue #12 lists issue #10 ("Apply data-quality fixes and regenerate
products.json") as a dependency. Issue #10 is still open — its own
acceptance criteria include "approved category cleanup mapping applied,"
which is tracked separately as **issue #39, also still open**. Concretely:
~8 stray categories (`Body Body`, `TRADEFAIR COMBO DEAL`,
`Glow serum`, `Dark spots & Discoloration serums`, `Eye cream`,
`Glow spray`, `sponge`, `Uncategorized`) still won't resolve to a planned
collection as things stand (risk #32, unchanged, `reports/phase9_collection_mapping.csv`).
**This means collection creation is blocked on real implementation work
(issue #39), not only on an approval decision** — approving collection
creation today would still leave those ~8 categories unmapped.

### 4.6 Collection creation plan (prepared, not executed)

Once approved and once issue #39 is resolved:

1. **Names/handles/types**: read directly from
   `shopify/foundation/collections.json` — already concrete, no further
   design work needed. Category collections use rule-based Shopify Smart
   Collections (`product_cat` membership); brand collections use
   rule-based Smart Collections (`Vendor equals {brand}`); the 3 manual
   promo collections are curated (product-list-based), matching
   `docs/SHOPIFY_FOUNDATION.md` § Manual promo collections exactly.
2. **Reuse `migration/scripts/phase9_dry_run.py`'s
   `build_collection_lookup()`/`resolve_collections()`** — already
   proven correct (151 of the catalog's real category/brand references
   resolve against `collections.json` today) — do not re-derive this
   logic in a new script.
3. **Build a dedicated Admin API collection-creation client**, following
   the same pattern already proven safe in
   `migration/scripts/phase9_test_import.py`: idempotent (match existing
   collections by handle before creating), checkpointed, structured
   logging, dry-run-first.
4. **Get explicit approval** (this is the actual gate, not a formality —
   nothing above authorizes creating anything).
5. **Run against the test store first**, same pattern as the product
   test: create the ~156-158 collections, verify handles/no-collisions/
   membership rules live, then assign a handful of the existing 9 test
   products to their expected collections and verify via fresh Shopify
   API queries — not performed this session, since approval doesn't
   exist yet (§ Step 6 was explicitly skipped for this reason).

## 5. Media — two decisions required (not made)

| | Current status | Option A | Option B |
|---|---|---|---|
| 14 AVIF assets (issue #6) | Proven safe to defer (test import skipped one cleanly) | Convert now, before bulk import | Defer — import without them, backfill later |
| 4 zero-image products (issue #8) | Proven safe to defer (test import handled one cleanly) | Source real images before import | Import without images |

12 duplicate media candidates (issue not yet filed for this specifically)
remain classified **D — handle after import** per the prior review; not a
decision blocking bulk import, since worst case is a redundant upload,
not corrupted data.

**Neither AVIF nor zero-image decision is made by this document.**

## 6. Outstanding decisions (consolidated)

1. Product 2371 (and 4 siblings, issue #38): real vendor, or explicit
   accept-blank approval.
2. Blank-vendor policy (~20 products, issue #41): accept Shopify's
   fallback, implement an alternative, or quarantine pending
   classification.
3. Collection creation approval — **and** completion of issue #39
   (category cleanup mapping), which is a real implementation
   prerequisite, not just a sign-off.
4. AVIF conversion timing (issue #6): now vs. deferred.
5. Zero-image products (issue #8): source images vs. import without.
6. Final, separate, explicit approval for the 602-product bulk import
   itself — distinct from all of the above, and from ADR-011 which only
   ever authorized the 9-product test.

## 7. Remaining products: 602

## 8. Bulk import: NOT PERFORMED

## 9. Bulk import authorization: REQUESTED

Posted explicitly on GitHub issue #14 (2026-08-10), listing all 6
outstanding decisions from § 6 and asking for them by name — not implied
by this document's existence alone, and this report itself grants
nothing. Per this task's absolute stop condition: **not APPROVED.**
Nothing in this repository authorizes bulk import; ADR-011 Decision 5
remains scoped to exactly the 9-product test.
