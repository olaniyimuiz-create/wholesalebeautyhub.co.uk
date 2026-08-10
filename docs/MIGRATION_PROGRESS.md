# Migration Progress

## Task tracker

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | SQL dump → products/customers CSV pipeline | Done | 611 products / 497 variations / 12,096 customers, validated end-to-end |
| 2 | Theme migration (Shopify Liquid theme) | Foundation built and accepted | `shopify/theme/` — 72 files, full commerce path, independently re-verified including a real Theme Check pass (0 offenses). See `docs/PHASE7_ACCEPTANCE.md`. Product/collection/customer import not started |
| 3 | Collections / navigation menus | Foundation ready | Concrete 156-collection list + navigation spec in `docs/SHOPIFY_FOUNDATION.md` and `shopify/foundation/`; theme templates built (`collection.json`/`.brand`/`.promo`), Admin API creation tracked in Milestone "Phase 9" |
| 4 | Blog + static pages | Foundation ready | 9 pages have existing Rank Math SEO copy worth preserving — see `docs/SEO_STRATEGY.md`; theme templates built (`blog.json`, `article.json`, `page.json`), content migration tracked as issue "Migrate blog posts and static pages" |
| 5 | SEO: URL redirect map (WooCommerce → Shopify slugs) | Done | 875-row redirect matrix + orphan/duplicate/broken-link reports in `reports/`; see `docs/SEO_STRATEGY.md` |
| 6 | Metafields / metaobjects for attributes not covered by variants | Foundation ready | Concrete schema in `docs/SHOPIFY_FOUNDATION.md` and `shopify/foundation/{metafields,metaobjects}.json`; theme reads `custom.brand`/`custom.included_items` already (graceful if unset); Admin API creation tracked as issue "Create metafield and metaobject definitions" |
| 7 | Shopify Admin/GraphQL API integration (automated import vs. CSV) | Approved (2026-08-10) | Admin API (GraphQL) — `docs/PHASE9_IMPORT_STRATEGY.md`, formally approved by the project owner as ADR-011 Decision 1. Test import executed against the dev store — `docs/PHASE9_ENVIRONMENT_READINESS.md` |
| 8 | Order history migration | Not started | Tracked as GitHub Milestone "Phase 11: Historical Order Strategy" (2 issues, ADR required before implementation) |
| 9 | Cutover plan (DNS, final sync, WooCommerce freeze) | Foundation ready | Import sequence, rollback strategy, and deployment checklist written in `docs/SHOPIFY_DEPLOYMENT.md`; implementation tracked in Milestone "Phase 13: Production Go-Live" |
| 10 | Media migration inventory (WooCommerce → Shopify) | Inventory built | 1,929-record canonical manifest, 8 CSV reports, idempotent and spot-checked — see `docs/MEDIA_MIGRATION.md`. Actual file conversion/upload not started — that's Phase 9 |
| 11 | Product import dry run (mapping, validation, data quality) | Dry run complete | 0 blocking issues (after 4 real pipeline fixes), idempotent, 7 reports — see `docs/PHASE9_PRODUCT_IMPORT.md`. No Shopify contacted; test/production import not started, blocked on store/credential provisioning |

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

### 2026-08-08 — Task 7: Theme Development (foundation)

- Pulled latest `main` and re-verified GitHub Issues/Milestones matched
  what Phase 6.5 created before starting (governance check for this
  phase) — no drift found.
- Found and resolved two real conflicts before building: WCAG 2.1 AA
  (existing GitHub issue) vs. WCAG 2.2 AA (this phase's instruction) —
  resolved by building to 2.2 AA, which is a superset; and undecided
  Markets/multi-currency/B2B business requirements — resolved by building
  the theme code-ready for them (no hardcoded currency/locale/audience
  assumptions) without inventing market configuration or B2B UI nobody
  has actually specified. Full reasoning in `docs/THEME_ARCHITECTURE.md`
  § Conflicts found and resolved.
- Built `shopify/theme/`: 72 files — full Online Store 2.0 structure
  (JSON templates, 2 section groups, 2 native Theme Blocks, 25 sections,
  21 snippets, design-token CSS + component CSS, a vanilla-JS behavior
  layer with no framework runtime). Covers the full commerce path plus
  search, navigation, blog, pages, and a starting account flow.
- Built directly against Phase 6.5's concrete specs, not generic
  placeholder content: `collection.brand.json` sources its hero from the
  `brand` metaobject with a graceful fallback (ADR-006 phasing),
  `collection.promo.json` omits Vendor/Product Type filters (ADR-007 —
  promo collections aren't taxonomy-driven), `product.combo-deal.json`
  surfaces the `custom.included_items` metafield.
- Ran real validation, not self-assessment: parsed every JSON file and
  embedded `{% schema %}` block, cross-referenced every locale key and
  every `render`/section-`type` reference against the files that should
  satisfy them. Caught and fixed 2 real bugs (hand-built JSON-LD via
  string concatenation in two structured-data snippets — would have
  broken on titles/URLs containing quote characters) and ~21 missing
  locale-key entries introduced while building forward without syncing
  the locale file. Full detail: `docs/PHASE7_REPORT.md` § Validation
  Report.
- Attempted to install Shopify CLI for real Theme Check + local preview;
  blocked by a Windows admin-elevation prompt this environment can't
  click through non-interactively. Disclosed as an environment gap in
  the risk register (#21) rather than silently skipped or faked.
- Wrote `docs/THEME_ARCHITECTURE.md`, `docs/COMPONENT_LIBRARY.md` (every
  component's status, usage, dependencies, and known limitations — 2
  components explicitly deferred with reasoning: Quick View and a full
  Quick Add flyout, both would duplicate the product page's own logic
  behind a second, likely shallower implementation), `docs/THEME_CHANGELOG.md`,
  and `docs/PHASE7_REPORT.md` (the 13-item Phase 7 deliverable set).
- No product/customer/order import and no deployment — per this phase's
  explicit scope. GitHub issue "Review wp_snippets custom code" remains
  open (requires reading live WooCommerce data, separate from theme
  building) — the other 4 Phase 7 issues closed.

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

## Phase 7 → Phase 8 Readiness

Full detail in `docs/PHASE7_REPORT.md` § 13. Summary: **ready to proceed
to Phase 8 (Media Migration)**. Phase 8's scope (image format conversion,
CDN migration, resolving zero-image products) operates on the WooCommerce
media library independently of the theme — nothing built this phase
blocks it, and `snippets/product-media.liquid`/`product-card.liquid` will
render whatever Phase 8 produces without changes. One recommendation, not
a blocker: run real Shopify Theme Check (risk #21) before more theme work
accumulates, once Node.js can be installed with proper elevation.

**Superseded by the formal acceptance review below** — that recommendation
about Theme Check has since been acted on (it now runs cleanly), and the
Phase 8 gate decision was revisited with a real, non-rubber-stamp
condition attached. This section is kept as the historical record of what
Phase 7 itself concluded; it isn't rewritten.

### 2026-08-08 — Phase 7 acceptance review

Independent re-verification, not a rerun of Phase 7 itself — see
`docs/PHASE7_ACCEPTANCE.md` for full detail.

- Re-ran every structural check from scratch (JSON, schema blocks, locale
  keys, snippet/section/asset/block-type references) rather than trusting
  the Phase 7 report's claims — all passed, confirming the prior report
  was accurate, not just re-asserted.
- Unblocked risk #21 (Theme Check tooling) using Node.js's portable zip
  distribution, which needs no admin elevation. Real `shopify theme check`
  now runs: found and fixed 2 genuine errors (missing required fields in
  `config/settings_schema.json`), then confirmed 68 files / 0 offenses.
- Formalized the Markets/B2B/multi-currency question as `docs/DECISIONS.md`
  ADR-010 — an explicit open "Business Decision Required" record instead
  of informal prose in `docs/THEME_ARCHITECTURE.md`.
- Actually read `wp_snippets`' real code (not just row metadata) for the
  first time — all 24 real snippets classified KEEP/REPLACE/RETIRE/
  INVESTIGATE. Surfaced two genuine new findings: brand logo images live
  in `wp_termmeta` (`pwb_brand_image`), not previously in Phase 6.5's
  asset inventory and now required input to Phase 8; and 24 specific
  product IDs the site owner had already flagged for price-data-integrity
  issues before migration, worth spot-checking before Phase 9 import.
- Added risks #24–26 and updated the status of #17, #21, #22 in
  `docs/RISK_REGISTER.md` to reflect what was actually resolved this
  review vs. what remains genuinely open.
- **Phase 8 gate: CONDITIONAL GO**, not a clean GO — see
  `docs/PHASE7_ACCEPTANCE.md` for the full reasoning. The condition:
  Phase 8's image inventory must include the `wp_termmeta` brand-logo
  source alongside `wp_posts` attachments, since that source wasn't
  identified until this review and missing it would mean re-scanning
  later rather than including it up front.
- No Phase 8 work was performed — inventory, conversion, and migration
  are explicitly still Phase 8's job, not this review's.

### 2026-08-08 — Task 8: Media Migration inventory

Analysis and reporting only — no files uploaded to Shopify, no
WooCommerce data modified. Full detail: `docs/MEDIA_MIGRATION.md`.

- Regenerated `migration/data/products.json` fresh (not reused) before
  building the inventory, per this phase's explicit instruction not to
  copy forward old figures.
- Built `migration/scripts/media_inventory.py`, reusing `sql_utils.py`
  like every other pipeline script. Independently recounted everything
  Phase 6.5 had estimated: 1,719 attachments and the mime-type/AVIF
  breakdown matched exactly (a consistency check passing, not an
  unverified assumption); the "1,123 used" figure is superseded by a
  more granular 1,929-usage-record manifest (one record per source+usage
  pair, not one per physical file).
- **Corrected, not just resolved, a Phase 7 finding**: real-data
  investigation (not code-reading) found `wp_termmeta` has zero rows for
  any of the 166 `pwb-brand` terms — brand logos were never actually
  populated. Risk #25 updated to reflect this is a correction, not new
  information layered on top of the old claim.
- Found a related, real, previously-unknown fact in the same
  investigation: category thumbnails (`product_cat` terms) are also 0-for-46
  populated. And a genuinely useful bonus finding: real hex color data
  exists for all 332 shade/color attribute terms that Phase 7's theme
  build didn't know about and used text-pill swatches instead of.
- Caught and fixed a real bug mid-build: WordPress's `'0'` = "not set"
  convention was initially mishandled as a literal attachment ID,
  producing 148 false "missing" variant images before the fix (194 fewer
  false positives after correcting it — final missing/broken count: 9,
  all genuine).
- Verified idempotency by running the full pipeline twice — all 8 CSV
  reports and the JSON manifest were byte-for-byte identical.
- Spot-checked 6 randomly sampled files against the live site: 5/6 exact
  filesize match, 1/6 revealed a real discrepancy (likely a file replaced
  outside the WordPress Media Library UI) — reported as a new risk, not
  hidden.
- Added risks #27–29 to `docs/RISK_REGISTER.md`; corrected #25.
- No Phase 9 work performed — no products, customers, or orders imported;
  no DNS/production changes; no WooCommerce data deleted.

### 2026-08-08 — Task 9: Product Import dry run

No Shopify store or API credentials exist anywhere in this project — this
phase is analysis, transformation, and validation only. Full detail:
`docs/PHASE9_PRODUCT_IMPORT.md`, `docs/PHASE9_IMPORT_STRATEGY.md`.

- Recommended Admin API (GraphQL) over CSV for the real import — deterministic
  upsert by ID, metaobject-reference support, structured error handling —
  conditional on a store/credentials existing, which they don't yet.
- Fixed 3 real, previously-known, already-approved pipeline bugs at the
  source rather than working around them in the dry run: Product Type
  (risk #9, now uses true category depth), ADR-007 brand exclusion
  (Valentine/Tradefair), and the Topicrem brand-name correction —
  investigating the last one properly found it was 2 products, not 4, as
  previously estimated.
- Found and fixed a new bug via the dry run's own duplicate-handle check:
  12 draft products had a completely empty handle (normal WordPress
  behavior for unpublished drafts) that would have silently merged into
  one product on import. Also fixed an HTML-entity/case bug in the dry
  run's own collection-name matching (not a source-data problem).
- Investigated GitHub issue #34 (price integrity) for real: confirmed all
  24 flagged IDs, found the pattern is actually broader (96 of 611
  products), and assessed the actual import impact (none — compare-at
  price is correctly left blank, not fabricated).
- Investigated the colour/shade data behind issue #37 with real numbers:
  132 of 326 variation-level shade assignments have real hex color data,
  184 reference a term with no color, 10 are orphaned. Documented as a
  proposed mapping, not implemented - needs approval.
- Ran the full dry run: 611 products, 519 published, 497 variants, 382
  data-quality issues found (0 blocking after the fixes above), 151
  collections/brands resolved, 31 genuinely unmapped (mostly Level 3
  categories that correctly become Product Type instead, not a bug).
  Verified idempotent - two full runs produced byte-identical output
  across all 7 reports.
- 5 products quarantined (not guessed) for an unresolvable "unnamed"
  placeholder vendor - a real ambiguous business classification, exactly
  the kind of decision this phase's governance says not to make silently.
- Pre-import reconciliation (source vs. this dry run's transformed
  payload - explicitly not vs. a live Shopify store, since none exists):
  100% match on every counted category.
- **Did not proceed to Steps 16/18** (test/production import) - both
  require a Shopify store and Admin API credentials that don't exist in
  this project. This is a real, external prerequisite, not a decision
  this phase could make or work around.

### 2026-08-08 — Pre-import environment readiness (supporting Phase 9, not Phase 10)

No Shopify store or credentials exist in this project, unchanged from the
dry run above. This step builds the readiness tooling/documentation for
when they do, without pretending they exist now. Full detail:
`docs/PHASE9_ENVIRONMENT_READINESS.md`.

- Independently re-verified every Phase 9 dry-run number against
  `migration/data/products.json` from scratch (not trusted from the prior
  report) — all matched exactly. Re-ran the idempotency check in a
  separate temp copy of the environment — byte-identical to committed
  reports. No discrepancies found.
- Built the credential security design: `.env.example` (placeholders
  only, tracked) and `.gitignore` rules (`.env`, `.env.*`,
  `!.env.example`) so real credentials can never be committed by accident.
- Built `migration/scripts/phase9_preflight.py` — a read-only Admin API
  pre-flight check (auth, store identity, scopes, read access to
  products/collections/metaobjects). Performs zero mutations, ever. Ran it
  for real in this environment: it correctly reported `NOT_CONFIGURED`
  (exit code 2) rather than fabricating a pass, since no credentials
  exist here.
- Selected a 9-product test-import set from real `products.json` data
  (`reports/phase9_test_import_set.csv`), covering every required test
  criterion including the known ambiguous-vendor and price-integrity edge
  cases — no invented test products.
- Designed (not built — no import client exists yet) the import safety
  mechanics: idempotent matching by `custom.legacy_woo_id`, checkpointing,
  structured logging, bounded retry, duplicate detection, rollback.
- Designed the reconciliation report schema
  (`reports/phase9_reconciliation_template.csv`) — header only, zero data
  rows, since no test import has run. Explicitly not the same thing as
  `reports/phase9_reconciliation.csv` (Phase 9 dry run), which compares
  source data against itself, not against a live store.
- Human approval gate checked against reality: 0 of 8 required items are
  met (no dev/test store, no credentials, no plan-tier decision, no
  Markets/B2B decision, test set not yet approved by the store owner).
- **Status: BLOCKED.** Did not create a Shopify store, request an Admin
  API token, perform any write against Shopify, import any product,
  customer, or order, or touch WooCommerce/DNS/production in any way.

### 2026-08-10 — Credential verification: store provisioned, pre-flight passes

The store owner provisioned a real Shopify development store and supplied
credentials across three attempts. Each was independently verified rather
than trusted: a placeholder token (`shpat_xxxx...`, wrong variable name)
was rejected without attempting a request; a real-shaped but invalid
token failed authentication for real (`401 Unauthorized`, all 5 checks);
a corrected token passed all 5 read-only pre-flight checks, re-run and
confirmed independently rather than accepted from the reported terminal
output alone. Full detail: `docs/PHASE9_ENVIRONMENT_READINESS.md`.

- Verified live: shop "Wholesale Beautyhub"
  (`wholesale-beautyhub.myshopify.com`), plan "Grow App Development", all
  9 required scopes granted, read access to products (0 - empty store),
  collections (1), and metaobject definitions (none yet) all confirmed.
- Human approval gate: **3 of 8 items now met** (store, credentials,
  permissions) — up from 0/8. Import method approval, plan-tier decision,
  Markets/B2B decision, test-set approval, and test-import approval
  remain undocumented and unmet; a passing connection was explicitly not
  treated as approval to proceed to a test import.
- **Status: BLOCKED — on approval, not infrastructure.** No write was
  attempted against Shopify at any point; no test import was performed.

### 2026-08-10 — Business approval (ADR-011) and first controlled test import

The project owner gave five explicit decisions (import method, UK-only
scope, test store, test set, test-import authorization) — recorded as
`docs/DECISIONS.md` ADR-011 before any write was attempted, not
after. Approval gate moved from 3/8 to 7/8 (only production plan tier
remains, and it doesn't gate this test).

Built `migration/scripts/phase9_test_import.py` (write client) and
`migration/scripts/phase9_test_reconcile.py` (independent live-verification
client), both reusing `phase9_dry_run.py`'s product loading/quarantine
rules and `phase9_preflight.py`'s credential/GraphQL helpers rather than
duplicating them.

- Ran the real 11-point pre-write preflight: all checks passed.
- Executed the test import: 8 of 9 products created; 1 quarantined
  (product 2371, the known ambiguous-vendor placeholder — the safety rule
  working as designed, not a failure).
- Independent live reconciliation (`phase9_test_reconcile.py`, fresh
  Shopify queries, not the import script's own report): 134 field-level
  comparisons, 131 matched, 3 real mismatches — reported, not hidden.
  Both root causes are new, real, previously-undiscoverable findings
  (only surfaced by a real API call): product 1721 has a blank variation
  attribute name Shopify rejected; blank-vendor products (20 of 611) get
  the shop name auto-filled as Vendor rather than staying blank. Added as
  risk register #33/#34 and GitHub issue #41 — not fixed silently.
- Idempotency verified for real: re-ran the identical import a second
  time. 0 products created (8 before, 8 after, independently re-queried),
  0 duplicate variants, 0 duplicate media.
- **Did not** proceed to the remaining 602 products, any customer, any
  order, or any production write — ADR-011 Decision 5 authorized exactly
  these 9 products and no more; that boundary was respected.

### 2026-08-10 — Post-test review: 2 defects fixed, 2 new findings, still blocked

Full detail: `docs/PHASE9_POST_TEST_REVIEW.md`. Investigated all 3 live
reconciliation mismatches from the initial test import individually
rather than treating them as one batch to explain away.

- **Product 1721 root-caused for real**: the raw `_product_attributes`
  serialized data shows the real WooCommerce attribute name is the
  literal character `%` (matches its `20%`/`10%` values) - the parser was
  reading the wrong field (the sanitized array key, which is empty for a
  name of just `%`) instead of the real `name` field. Fixed in
  `database_parser.py` with a minimal, evidence-scoped fallback; verified
  against the 9 other custom-attribute products in the catalog to confirm
  zero regressions, and re-verified the full dry-run pipeline produces
  identical summary numbers.
- **Product 2371 investigated exhaustively**, including a fresh raw scan
  of all 257 `wp_postmeta` rows directly against `dump.sql`: no real
  vendor signal exists anywhere. Remains correctly quarantined - not
  guessed.
- **Vendor auto-fill (product 7535)**: confirmed as a real Shopify
  platform behavior (shop name fills blank vendor fields), not a defect
  in this project's code. Left as an open, explicit business decision -
  not silently worked around.
- **New defect found while fixing the above**: the importer never set
  real inventory quantities (all created variants showed `0` regardless
  of source `stock_quantity`), and untracked-inventory items were being
  marked `tracked` unconditionally. Fixed the `manage_stock` gating; the
  quantity-setting fix itself then hit a real, current Shopify API
  requirement (`inventorySetQuantities` needs an `@idempotent` directive
  not implemented here) - left honestly failing and logged, not
  papered over. Also fixed a real bug where this exact kind of failure
  was being silently swallowed (call site never checked the mutation's
  return value).
- Re-ran the full validation cycle after every change: dry-run pipeline
  (no regressions), test import (0 created, 8 updated, 1 quarantined),
  live reconciliation (146 of 161 field comparisons now match, up from
  131 of 134 - more total checks because inventory-quantity comparison
  was added), and idempotency (re-verified twice more, still no
  duplicates of any kind).
- **Status: BLOCKED for bulk import** - unchanged in substance, but the
  blocker moved from "outstanding defects" to "business decisions +
  one real API gap," which is real progress, not just relabeling.

### 2026-08-10 — Risk #35 (inventory) resolved and verified live

Full detail: `docs/PHASE9_INVENTORY_FIX_REPORT.md`. Fetched real Shopify
documentation directly (not from memory) to confirm the root cause rather
than guessing again: `inventorySetQuantities` requires a mandatory
`@idempotent(key: $key)` directive as of API 2026-04, plus a
`changeFromQuantity` value per entry (confirmed by live testing, since
the fetched docs summary was imprecise on this second point). Fixed in
`migration/scripts/phase9_test_import.py`.

- Built and ran `migration/scripts/test_phase9_inventory.py` - 9
  scenarios (8 live-integration, 2 mocked failure modes), all passing for
  real. Found and fixed 2 related real bugs the test suite itself
  surfaced: crashes on a missing inventory item and on a malformed API
  response.
- Re-ran the full production import: live reconciliation improved from
  134/148 matched (13 inventory mismatches) to **160/161 matched, 0
  inventory mismatches** - the only remaining mismatch is the unrelated,
  already-documented vendor auto-fill.
- Idempotency re-verified twice more after the fix: no duplicate
  products, variants, or media at any point, independently re-queried
  each time, not just trusted from the importer's own log.
- Product 7535's vendor auto-fill investigated to a confirmed root cause
  (Shopify platform behavior, not importer or source-data behavior) -
  left open as a business decision, no vendor invented or changed.
- **Status: still BLOCKED for bulk import** - the technical inventory gap
  is closed; what remains is the vendor business decision, collection
  creation (separate scope), and the human approvals already listed in
  the prior review. Bulk import was not requested or performed.

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
