# Migration Progress

## Task tracker

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | SQL dump → products/customers CSV pipeline | Done | 611 products / 497 variations / 12,096 customers, validated end-to-end |
| 2 | Theme migration (Shopify Liquid theme) | Not started | Tracked as GitHub Milestone "Phase 7: Theme Development" (5 issues) |
| 3 | Collections / navigation menus | Foundation ready | Concrete 156-collection list + navigation spec in `docs/SHOPIFY_FOUNDATION.md` and `shopify/foundation/`; implementation tracked in Milestone "Phase 9" |
| 4 | Blog + static pages | Foundation ready | 9 pages have existing Rank Math SEO copy worth preserving — see `docs/SEO_STRATEGY.md`; tracked as issue "Migrate blog posts and static pages" |
| 5 | SEO: URL redirect map (WooCommerce → Shopify slugs) | Done | 875-row redirect matrix + orphan/duplicate/broken-link reports in `reports/`; see `docs/SEO_STRATEGY.md` |
| 6 | Metafields / metaobjects for attributes not covered by variants | Foundation ready | Concrete schema in `docs/SHOPIFY_FOUNDATION.md` and `shopify/foundation/{metafields,metaobjects}.json`; tracked as issue "Create metafield and metaobject definitions" |
| 7 | Shopify Admin/GraphQL API integration (automated import vs. CSV) | Not started | Still undecided — blocks the exact acceptance criteria for the "Import products" issue in Milestone "Phase 9" |
| 8 | Order history migration | Not started | Tracked as GitHub Milestone "Phase 11: Historical Order Strategy" (2 issues, ADR required before implementation) |
| 9 | Cutover plan (DNS, final sync, WooCommerce freeze) | Foundation ready | Import sequence, rollback strategy, and deployment checklist written in `docs/SHOPIFY_DEPLOYMENT.md`; implementation tracked in Milestone "Phase 13: Production Go-Live" |

## Change log

### 2026-08-07 — Task 1: data pipeline

- Inspected the real Adminer dump (`wp_posts`, `wp_postmeta`,
  `wp_wc_product_meta_lookup`, `wp_wc_customer_lookup`,
  `wp_wc_order_addresses`, term tables) to build the parser against actual
  schema and data rather than assumptions.
- Built `migration/scripts/sql_utils.py`, `database_parser.py`,
  `csv_generator.py`.
- Ran end-to-end against the live export: 1.6M rows scanned in ~19s, 611
  products / 497 variations / 12,096 customers, 0 malformed CSV rows
  (verified by re-parsing both outputs with Python's `csv` module).
- Added `.gitignore` for the dump, parsed JSON, and generated CSVs — all
  contain real customer PII.

### 2026-08-07 — Task 5: SEO & URL mapping

- Confirmed WooCommerce's actual permalink structure from `wp_options`
  (`/shop/` product base, `/product-category/`, `/product-tag/`) and
  spot-checked 4+ live pages to validate it, catching one case where the
  database's implied URL (a nested page path) didn't match what's actually
  served (ADR-005).
- Built `migration/scripts/seo_url_mapper.py` (reuses `sql_utils.py` and
  the existing `products.json`) to inventory products, categories, brands,
  tags, pages, and blog posts, and generate:
  `reports/redirect_matrix.csv` (875 rows), `reports/duplicate_urls.csv`,
  `reports/orphan_urls.csv`, `reports/broken_links.csv`.
- Parsed `wp_posts` `nav_menu_item` + `_menu_item_*` postmeta to check
  real navigability instead of only in-content links — cut the false
  "orphan" count from 872/875 to a defensible 80/875 by correctly
  excluding products/tags (normally not in a nav menu), WooCommerce
  utility pages (cart/account/checkout — reachable via header UI, not
  menus), and pages confirmed live in the theme's hardcoded footer.
- Found and documented: one real category/brand slug collision, the
  tag→collection mapping gap, the blog not being linked from the header
  menu, and a pre-existing wrong meta description on the live Cookie
  Policy page. Full detail in `docs/SEO_STRATEGY.md`.
- Wrote `docs/SEO_STRATEGY.md` and `docs/DECISIONS.md` (ADR-001 through
  ADR-005); moved the risk register out to `docs/RISK_REGISTER.md` and
  extended it with the findings above.

### 2026-08-07 — Task 6: Shopify Information Architecture

- Validated Phase 5 outputs before starting (see Readiness Assessment
  below), then investigated the blocking slug collision at the data level
  rather than the URL level: the 8 products behind "VALENTINE COMBO DEALS"
  are Valentine's Day gift bundles, not products from a brand — same
  pattern confirmed in "TRADEFAIR COMBO DEALS". Resolved by reclassifying
  both as manual collections instead of a handle-naming workaround
  (ADR-007).
- Compared 4 brand-architecture options (Vendor only / Smart Collection /
  Metaobject / Hybrid) against 10 criteria; recommended and adopted the
  phased hybrid (ADR-006).
- Mapped WooCommerce's 3-level category tree onto Shopify's flat collection
  model: top level → nav structure, Level 2 → real collections, Level 3 →
  Product Type filter, not further collections (ADR-009). Found ~12
  categories miscategorized at WooCommerce's top level and proposed a
  cleanup mapping pending merchant sign-off.
- Wrote `docs/SHOPIFY_ARCHITECTURE.md` (taxonomy, collections, navigation,
  product types, vendors, tags, metafields, metaobjects, Search &
  Discovery filters, URL/handle conventions, collection/product templates,
  brand pages) and ADR-006 through ADR-009 in `docs/DECISIONS.md`.
- Extended `docs/RISK_REGISTER.md`: resolved the slug-collision risk,
  added 6 new findings (Product Type column needs a Phase 9 fix, category
  cleanup needs sign-off, 8 bundle products need a manual vendor check,
  tag casing inconsistency, informational note on the 37 brands with no
  live products).
- No import scripts, theme code, or production data were touched — this
  phase was architecture and documentation only, per the explicit scope
  given for it.

### 2026-08-07 — Task 6.5: Shopify Foundation

- Re-read all six architecture docs and cross-checked their claims against
  `reports/*.csv` before starting — no drift found (see Phase 6.5
  Readiness Report below for what was specifically checked).
- Compiled the approved architecture into concrete, buildable specs:
  `docs/SHOPIFY_FOUNDATION.md` (information model, 156-collection list
  with real product counts, navigation structure, metafield/metaobject
  schema, Product Type strategy, Search & Discovery filter design, media
  asset inventory, WooCommerce→Shopify app replacement matrix built from
  the site's actual active-plugin list) plus machine-readable versions
  under `shopify/foundation/` (`collections.json`, `metafields.json`,
  `metaobjects.json`, `navigation.json`) for Phase 9 tooling to consume.
- Found 7 new data-quality issues while compiling the concrete lists
  (added as risks #14–20): a brand term with a garbage name/slug, 3
  near-duplicate misspelled brand entries for what's almost certainly one
  brand, 14 AVIF images needing format conversion, unreviewed custom code
  snippets, two simultaneously-active email marketing plugins, an
  unconfirmed POS plugin status, and two AI-integration plugins with
  unclear purpose.
- Wrote `docs/SHOPIFY_BUILD_GUIDELINES.md` (how Phase 7 should proceed),
  `docs/SHOPIFY_CODING_STANDARDS.md` (Liquid/JS/CSS conventions, Theme
  Check requirement), and `docs/SHOPIFY_DEPLOYMENT.md` (import sequence,
  rollback strategy, deployment checklist, Shopify CLI workflow).
- Created the `/shopify/` directory (`foundation/`, `theme/`, `scripts/`
  — the latter two intentionally empty/reserved, per this phase's
  explicit no-theme-code scope).
- Converted the remaining roadmap (Phases 7–13) into 7 GitHub Milestones
  and 30 Issues, each with explicit acceptance criteria and a "Depends
  on" section reflecting the real dependency chain in
  `docs/SHOPIFY_DEPLOYMENT.md`'s import sequence — not just a flat task
  list.
- No theme code, imports, or production data were touched — architecture
  compilation and repository governance only, per this phase's explicit
  scope.

## Phase 6.5 Readiness Report

**Consistency check performed**: re-read `docs/ARCHITECTURE.md`,
`docs/SEO_STRATEGY.md`, `docs/SHOPIFY_ARCHITECTURE.md`, `docs/DECISIONS.md`,
and `docs/RISK_REGISTER.md` in full; cross-checked every number they cite
(875 redirects, 156 planned collections, 129/127 brand counts, 23/28
category collections, product/customer totals) against the underlying
`reports/*.csv` and a fresh query of `migration/sql/dump.sql` rather than
trusting the documents' own prior claims. Everything traced correctly; no
contradictions found between what was approved (ADR-006, ADR-007, the
category cleanup mapping) and what's written down.

**Blockers resolved this phase**: the three items listed as open in the
Phase 6 Readiness Assessment are now closed — ADR-006, ADR-007, and the
category cleanup mapping are all approved and reflected in
`docs/SHOPIFY_FOUNDATION.md`'s concrete lists.

**New items found while making the architecture concrete** (none block
Phase 7 starting, all are gating for Phase 9 — see risks #14–20 and
`docs/RISK_REGISTER.md` for the full list): a garbage-data brand term, a
3-way duplicate brand spelling, 14 images in an unsupported format, and
four "needs a conversation with the client" items (unreviewed code
snippets, two competing email tools, POS plugin status, two AI plugins of
unclear purpose).

**Ready to proceed to Phase 7 (theme development)?** Yes, with one caveat:
`docs/SHOPIFY_FOUNDATION.md`'s metafield/metaobject schema and collection
template plan are stable enough to build theme code against now. Nothing
in Phase 7's scope (issues #1–5, Milestone "Phase 7") depends on the
Phase 9-gating data-quality fixes (risks #14–20) — those affect what data
gets imported, not how the theme renders it. The one thing worth doing
before or alongside starting Phase 7: issue #5 (review `wp_snippets`
content), since it could surface storefront behavior that changes what
Phase 7 needs to build, and it's cheap to check now rather than mid-build.

**Not yet decided, tracked, not blocking**: the CSV-vs-Admin-API question
(task 7) and the order history strategy (Milestone "Phase 11") — both
have dedicated issues and don't gate Phase 7.

## Phase 6 Readiness Assessment

**Phase 5 consistency check** (before starting Phase 6): re-read
`reports/redirect_matrix.csv`, `duplicate_urls.csv`, `orphan_urls.csv`,
`broken_links.csv` against `docs/SEO_STRATEGY.md`'s claims — counts match
(875 redirects, 1 collision, 80 items needing review, 0 broken links) and
every number cited in the strategy doc traces to a report row. No drift
found.

**Ready to proceed to Phase 7 (theme development)?** Not yet — the
architecture is designed and internally consistent, but three things need
a human decision before implementation should start:

1. Sign off on the Option D (hybrid) brand architecture (ADR-006) and the
   phasing — metaobject content layer after Phase 7, not before.
2. Sign off on the category cleanup mapping (`docs/SHOPIFY_ARCHITECTURE.md`
   § Category hierarchy) — it's a content decision, not something to
   infer automatically.
3. Confirm the reclassification of the 2 promo-bundle "brands" (ADR-007)
   and, separately, decide whether the 8 affected products get a real
   Vendor value or stay blank.

Nothing else is blocking. The redirect matrix, tag/vendor mapping, product
type strategy, and template plan don't depend on those three decisions and
can move into Phase 7/9 as soon as they're made.

## Risk register

Moved to [`docs/RISK_REGISTER.md`](RISK_REGISTER.md).
