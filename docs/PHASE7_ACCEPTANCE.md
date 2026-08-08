# Phase 7 Acceptance Review

Independent verification of Phase 7 (commit `ac6665d`), performed by
re-running every check from scratch rather than trusting
`docs/PHASE7_REPORT.md`'s claims. Where this document's findings differ
from that report (Theme Check now runs; two new findings from actually
reading `wp_snippets`), this document is authoritative — `PHASE7_REPORT.md`
is left as the historical record of what Phase 7 itself reported, not
retroactively edited.

## 1. Executive Summary

Phase 7's implementation is confirmed complete and sound. Every
structural/reference check that can be run without a live Shopify store
passes, including — new since the last report — a real Shopify Theme
Check run (previously blocked, unblocked this review via a portable
Node.js binary that doesn't require admin elevation). One genuine defect
was found and fixed by Theme Check (two missing required fields in
`config/settings_schema.json`); zero defects remain. Two of the three
previously-open Phase 7 issues were investigated for real rather than left
as speculation: the Markets/B2B question is now a formal decision record
(ADR-010) instead of informal prose, and `wp_snippets`' actual code (not
just row metadata) was extracted and classified, surfacing two genuine
new findings (a brand-logo data source for Phase 8, and evidence of a
pre-existing price-data-integrity investigation on 24 specific products).
No Phase 8 work was performed.

## 2. Implementation Verification

| Item | Verified | Result |
|---|---|---|
| Commit `ac6665d` exists on `origin/main` | `git log origin/main --oneline` | Confirmed present |
| Working tree clean | `git status` after `git pull` | Confirmed clean |
| Theme file count | `find shopify/theme -type f \| wc -l` | 72 (matches prior report exactly) |
| File breakdown | Counted per directory | assets: 3, blocks: 2, config: 2, layout: 1, locales: 2, sections: 25, snippets: 21, templates: 15 (incl. `customers/`) |
| Required docs present | Checked all 14 listed in this review's instructions | All present |

## 3. Repository Verification

`git pull origin main` reported "Already up to date"; `git log` and
`git log origin/main` show identical history with `ac6665d` as HEAD. No
divergence between local and remote.

## 4. Theme Structure Verification

Matches the Online Store 2.0 structure documented in
`docs/THEME_ARCHITECTURE.md` § Folder structure: JSON templates
throughout except `templates/customers/*.liquid` (Shopify's own
constraint, not a gap), 2 section groups (`header-group`, `footer-group`),
2 native Theme Blocks. No stray or orphaned files found outside the
expected structure.

## 5. Liquid Validation

**Embedded `{% schema %}` blocks**: 21 found, all valid JSON (re-parsed
independently — see § 8 for method).

**Real Theme Check** (`shopify theme check`, Shopify's own official
linter — not available in the previous review, unblocked this time):

```
68 files inspected with no offenses found.
```

First run found 2 errors (missing `theme_support_email` and
`theme_documentation_url` in `config/settings_schema.json`'s `theme_info`
block) — fixed, re-run confirms 0 offenses. This is the one code change
made during this review, and it qualifies as fixing a genuine discovered
defect, not unrequested alteration.

## 6. JSON Validation

All 18 standalone `.json` files (templates, config, locales) parse via
Python's `json.load` with zero failures — re-run independently this
review, same result as Phase 7's own report.

## 7. Translation Validation

Storefront (`| t`) and schema (`t:`) locale references cross-checked
against `locales/en.default.json` / `en.default.schema.json`:

| Reference type | Total references | Unresolved |
|---|---:|---:|
| Storefront `'key' \| t` | 111 | 0 |
| Schema `"t:key"` | 67 | 0 |

(Pluralization objects — e.g. `cart_count: {one, other}` — correctly
treated as valid leaf keys, matching Shopify's actual `t` filter
behavior, not flagged as false positives.)

## 8. Component Validation

Cross-referenced every `{% render 'x' %}` against `snippets/*.liquid`
(71 references, 0 unresolved), every template/section-group `"type"`
against `sections/*` (23 references, 0 unresolved — checked precisely
against top-level section keys only, not block-level `"type"` values
which use a separate namespace), every `'file' | asset_url` against
`assets/*` (4 references, 0 unresolved), and — new this review — every
block `"type"` referenced inside a template's section `"blocks"` object
against that section's own schema-declared block types or, where the
section accepts `"@theme"`, against `blocks/*.liquid` (20 sections-with-
blocks scanned across all templates, 0 unresolved). No broken internal
references exist anywhere in the theme.

## 9. Accessibility Readiness

**NOT YET MEASURED.** No axe, Lighthouse, or screen-reader pass has been
run — there is no deployed store to test against, and this phase
explicitly excludes deployment. What exists is code-level construction
against WCAG 2.2 AA success criteria (semantic landmarks, skip link,
non-obscured focus states, 44px targets, focus-trapped dialogs, live-
region announcements, no-JS-required forms) — see
`docs/THEME_ARCHITECTURE.md` § Accessibility approach for the full list.
Construction-against-criteria is not the same claim as an audited pass.
The real audit is GitHub issue "Accessibility audit (WCAG 2.2 AA)" under
Milestone "Phase 12."

## 10. SEO Readiness

Product/CollectionPage/Organization/BreadcrumbList/BlogPosting JSON-LD,
OpenGraph/Twitter Card tags, and canonical URLs are implemented and
verified to produce syntactically valid output (the breadcrumb and
Organization schema were specifically rewritten during Phase 7's own
review to use the `json` filter per field after a string-concatenation
bug was found — see `docs/THEME_CHANGELOG.md`). Actual search ranking,
crawl behavior, and Search Console signals are **NOT YET MEASURABLE** —
nothing is live to be crawled.

## 11. Performance Readiness

**NOT YET MEASURED.** No Lighthouse score or Core Web Vitals numbers
exist. Built-in practices (responsive `image_tag` with explicit
`widths`/`sizes`, lazy-loading except first-paint images, single
deferred JS file, dynamic-section-fetched recommendations/search/
recently-viewed rather than inline bloat) are implemented per
`docs/THEME_ARCHITECTURE.md` § Performance approach, but "built with
performance practices" and "measured performant" are different claims —
only the first is being made here. The real audit is GitHub issue
"Performance audit" under Milestone "Phase 12."

## 12. Known Limitations

Unchanged from `docs/COMPONENT_LIBRARY.md` (Quick View and full Quick Add
not built, Testimonials has no source content, true color swatches need a
data decision not a theme decision, most customer account sub-pages
beyond login/register/order-history not built) — re-confirmed accurate,
nothing found to contradict it. Two new limitations surfaced this review:
no free-shipping-threshold notice exists yet (real WooCommerce feature,
not yet ported — see § "wp_snippets findings" below), and no dedicated
"Brand Logos" grid section exists (the current `collection-list.liquid`
renders collection images generically, not a brand-logo-specific
treatment the old site had).

## 13. Open Issues

Milestone "Phase 7: Theme Development" — re-checked live against GitHub,
not assumed:

| # | Title | State | This review's finding |
|---|---|---|---|
| 1–4 | Foundation/collection templates/product templates/navigation | Closed | Re-confirmed correctly closed — implementation verified in §§ 4–8 above |
| 5 | Review wp_snippets custom code | Open | **Actually investigated this review** (previously only "evidence unavailable" was possible without reading the dump) — see findings below. Remains open: several items are classified but not yet built or decided |
| 31 | Run Shopify Theme Check + CLI local preview | Open | Theme Check now passes (0 offenses) — **updating, not closing**: CLI local preview (`shopify theme dev`) genuinely requires a connected live Shopify store, which doesn't exist yet: a separate, real prerequisite, not the same blocker as Theme Check |
| 32 | Decide Markets, multi-currency, and B2B scope | Open | Formalized as ADR-010 this review; still genuinely undecided — correctly remains open |

### wp_snippets findings (issue #5)

Extracted and read the actual `code` column for all 24 real snippets in
`wp_snippets` (4 additional rows are the Code Snippets plugin's own
inactive sample content — not site logic, not classified). Classification
per this review's required KEEP/REPLACE/RETIRE/INVESTIGATE taxonomy:

| Snippet | Active | Class | Why |
|---|:-:|---|---|
| Auto-Sync Parent Variable Product Prices | Y | RETIRE | WooCommerce variation→parent price-sync bug workaround; Shopify variants don't have this failure mode |
| elementor (RevSlider dequeue) | Y | RETIRE | Page-builder conflict fix, WordPress-specific |
| Reassurance Strip on Checkout | N | REPLACE | Already built as `blocks/trust-badge.liquid` |
| Trust Badges Below Order Button | Y | REPLACE (placement differs) | Built as `blocks/trust-badge.liquid`, but placed on the product page — Shopify's non-Plus checkout doesn't accept theme Liquid on the checkout page itself; Plus-tier Checkout Extensibility would be needed to match the original placement exactly |
| Progress Bar on Checkout (CSS) | N | RETIRE | Shopify's own checkout already shows step progress natively |
| Guest Checkout Default | Y | RETIRE | Native Shopify Admin setting (Settings → Checkout), no code needed |
| Company Name Optional | Y | INVESTIGATE | Checkout field customization capability depends on Shopify plan (Plus vs. non-Plus) — **blocked on the same undecided plan tier as ADR-010** |
| Checkout Field Order | Y | INVESTIGATE | Same plan-tier dependency |
| Back to Cart Link | Y | INVESTIGATE | Same plan-tier dependency |
| Free Delivery Threshold Notice | Y | REPLACE — not yet built | Genuine gap. Real, valuable merchandising (not checkout-page-dependent — can be built into the cart drawer/page). Recommend a new Phase 9-or-later issue, not built during this review per "don't alter working code without a defect" |
| Thank You Page Message | Y | INVESTIGATE | Likely achievable via Shopify's order-status-page customization; exact mechanism is plan-dependent |
| Defer Google Fonts / Preconnect / Fix Render Blocking (3 snippets) | Y | RETIRE | Solve a WordPress self-hosted-fonts loading problem; Shopify's native `font_picker` already handles font loading |
| Homepage H1 SEO Heading (hidden-H1 hack) | Y | RETIRE — need already met | `sections/hero-banner.liquid` already renders a real, visible `<h1>` — better than the original's visually-hidden workaround, not a gap |
| Fix WooPayments Place Order AJAX Trigger | Y | RETIRE | WooCommerce/Stripe plugin DOM-ID bug; not applicable to Shopify's hosted checkout |
| Products by Category & Brand Logos Shortcodes | Y | REPLACE — partial + new finding | Category grid ≈ `sections/collection-list.liquid` (built). Brand Logos grid is **not yet built** as its own section. **Important finding**: the code reveals brand logo images are stored in `wp_termmeta` (key `pwb_brand_image`) on the `pwb-brand` taxonomy — a data source not previously identified in Phase 6.5's asset inventory. Feeds directly into the `brand` metaobject's `logo` field already speced in `shopify/foundation/metaobjects.json` — confirms that design was right, and gives Phase 8/9 a concrete source query |
| Hide WooCommerce Default Card Input, Stripe Only | Y | RETIRE | Shopify Payments (native, Stripe-powered) replaces this entirely; payment method visibility is an Admin setting, not code |
| Fix Place Order Redirect (Stripe/WooPayments loop) | N | RETIRE | WooCommerce+Stripe-specific redirect bug; doesn't exist on Shopify's hosted checkout |
| Show Checkout Error Messages (Stripe fix) | N | RETIRE | Same class of WooCommerce/Stripe-plugin-conflict bug |
| Fix WC_Checkout null error in Stripe REST API | Y | RETIRE | WooCommerce plugin-architecture conflict (duplicate Stripe.js loading); not applicable |
| Push Sold-Out Products to End of Catalog | Y | INVESTIGATE | Real, valuable behavior (out-of-stock items sort last regardless of chosen sort). Needs verification against current Shopify Search & Discovery capability — whether this is a native sort/deprioritization option or requires custom Liquid sort logic. Not verified this review (would require live-store testing, out of scope) |
| TEMP - Price Integrity Read-Only Diagnostic | Y (trashed) | RETIRE + **risk flag** | The diagnostic tool itself isn't needed post-migration. But it reveals the store owner was actively investigating **known price-data-integrity issues** (duplicate `_price`/`_regular_price`/`_sale_price` postmeta rows, product-meta-lookup/postmeta mismatches) on 24 specific WooCommerce product IDs before migration. New risk added — see § Risk Assessment |

No snippet content was found to be genuinely unrecoverable/unavailable —
"evidence unavailable" does not apply here; the dump contains the full
`code` column for every row.

## 14. Risk Assessment

New risks from this review (also being added to `docs/RISK_REGISTER.md`
as #24–26):

- **24 (Medium)**: 24 specific WooCommerce product IDs (`54, 55, 68, 69,
  84, 87, 90, 106, 108, 146, 244, 246, 1699, 1720, 1721, 1723, 1763, 2376,
  4673, 16445, 16482, 17478, 18756, 25122`) were flagged by the site
  owner's own diagnostic tooling as having potential price-data
  integrity issues (duplicate meta rows or lookup-table mismatches)
  before migration. These IDs' prices in `migration/data/products.json`
  should be spot-checked against the live site before Phase 9 import,
  not assumed correct.
- **25 (Low)**: Brand logo images exist in `wp_termmeta` (`pwb_brand_image`)
  and were not part of Phase 6.5's asset inventory — Phase 8 needs to
  include them, and they should feed the `brand` metaobject's `logo`
  field (`shopify/foundation/metaobjects.json`).
- **26 (Low)**: A "Free Delivery Threshold Notice" (real, active
  WooCommerce functionality) has no Shopify equivalent built yet — not a
  Phase 7 blocker, but a real content/merchandising gap for a future
  issue.

Existing risks re-confirmed accurate, none found stale or resolved
incorrectly (see `docs/RISK_REGISTER.md` for the full register).

## 15. Outstanding Decisions

1. **Markets / multi-currency / B2B** (ADR-010, this review) — genuinely
   undecided, formally recorded as open, does not block Phase 8.
2. **Shopify plan tier** (Plus vs. non-Plus) — newly surfaced as a
   concrete blocker for 3 of the `wp_snippets` INVESTIGATE items
   (checkout field customization requires Plus's Checkout Extensibility
   on current Shopify). Not previously tracked as its own decision;
   effectively a sub-question of ADR-010's B2B/scope discussion but
   worth naming explicitly since it also affects non-B2B checkout
   customization.
3. **CSV vs. Admin API for product import** (task 7 in
   `docs/MIGRATION_PROGRESS.md`) — still undecided, pre-existing, not
   new to this review.
4. **Order history migration approach** — still undecided, tracked as
   its own Milestone ("Phase 11"), not new to this review.

## 16. Phase 8 Dependencies

Phase 8 (Media Migration) depends on nothing unresolved in Phase 7.
`snippets/product-media.liquid` and `snippets/product-card.liquid` render
whatever standard Shopify media objects Phase 8 produces without
modification. The one new input Phase 8 should incorporate: brand logo
images from `wp_termmeta.pwb_brand_image` (§ 13 finding), not previously
in scope.

## Final Acceptance Recommendation

**ACCEPT Phase 7 as complete**, with the acceptance conditioned on the
open items in § 13 being tracked (not silently dropped) rather than
requiring rework before proceeding. No implementation defect was found
that isn't already fixed (§ 5). The theme foundation is sound, verified
by both independent structural re-analysis and, newly, real Theme Check
output — not solely by the phase's own self-report.

---
See § "Phase 8 Readiness Gate" in this review's accompanying response for
the GO/CONDITIONAL GO/NO-GO determination.
