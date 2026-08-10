# Architectural Decision Records

Format: short-form ADR (context, decision, consequences). Numbered
sequentially; never renumber or delete a past entry, even if superseded —
add a new one that references it instead.

## ADR-001: Build the URL inventory from the SQL dump, not a live crawl

**Context**: Phase 5 asked for a crawl of the live site to inventory URLs.

**Decision**: Derive the inventory from `dump.sql` (already validated in
Phase 3/4) instead. The database is a strictly better source: it includes
unpublished/draft content a crawler would never see (relevant to the orphan
report), gives exact WooCommerce permalink settings instead of inferring
them from observed pages, and doesn't put scraping load on the production
site. A handful of live pages were still fetched to confirm the inferred
patterns and catch cases where the DB doesn't match reality (see ADR-005).

**Consequences**: Misses anything not represented in the tables already
parsed — notably theme-hardcoded navigation (footer links, WooCommerce
account UI) that doesn't live in `wp_nav_menu` data. Documented as a known
limitation in `docs/SEO_STRATEGY.md` rather than silently producing a wrong
orphan report.

## ADR-002: Map WooCommerce brands to Shopify collections

**Context**: Shopify has no first-class "brand" entity distinct from
collections; WooCommerce brands (via the pwb-brand taxonomy, 166 terms) need
somewhere to live.

**Decision**: Represent each brand as a Shopify collection at
`/collections/{brand-slug}`, built (in a later phase) from products filtered
by vendor. This is the standard approach for WooCommerce brand plugins
migrating to Shopify.

**Consequences**: Category and brand collections share the same
`/collections/` namespace, which creates the slug collision documented as
finding #1 in `docs/SEO_STRATEGY.md`. Must be resolved before Phase 6
collection creation.

## ADR-003: Product tags fall back to `/collections/all`

**Context**: Shopify has no tag-archive page equivalent to WooCommerce's
`/product-tag/{slug}/`.

**Decision**: Redirect all 103 tag URLs to `/collections/all` by default —
a working, non-broken destination — rather than inventing a collection per
tag speculatively.

**Consequences**: Loses tag-specific landing pages. Acceptable default;
flagged in `docs/SEO_STRATEGY.md` for a pre-cutover Search Console traffic
check in case a handful of tags are worth promoting to real collections.

## ADR-004: Shopify blog handle defaults to `news`

**Context**: WooCommerce's blog lives at a custom page slug
(`wbh-beauty-blog`); Shopify blogs are addressed by handle
(`/blogs/{handle}`).

**Decision**: Use Shopify's conventional default handle, `news`, until
Phase 7 (theme development) decides otherwise.

**Consequences**: Trivial to change later — only affects the `new_path`
column for 2 blog posts and the blog index; re-run
`migration/scripts/seo_url_mapper.py` if the handle changes before the
redirect matrix is imported.

## ADR-005: Trust a live-observed URL over the database's implied one

**Context**: `wp_posts.post_parent` for "Refund and Returns Policy" implies
a nested URL (`/wholesalebeautyhub-2/refund_returns/`) under WordPress's
normal hierarchical page permalinks. Fetching that URL live returns the page
at the flat `/refund_returns/` instead, most likely because WooCommerce
registers its own core policy pages by option ID rather than through page
hierarchy.

**Decision**: `seo_url_mapper.py` treats all WordPress pages as flat
(`/{slug}/`), full stop — no parent-path logic. When the schema and the live
site disagree, the live site wins, because that's what's actually indexed
and what the redirect has to match.

**Consequences**: Correct for this store's only hierarchical page. Revisit
if a future page turns out to genuinely need a nested URL (none currently
do).

## ADR-006: Brand architecture — hybrid Vendor + Smart Collection now, Metaobject content layer as fast-follow

**Context**: Phase 6 needs a long-term answer for how WooCommerce's 166
`pwb-brand` terms (129 with at least one published product) become Shopify
concepts. Four options were compared — full analysis in
`docs/SHOPIFY_ARCHITECTURE.md` § Brand Architecture:

| | Vendor only | Smart Collection | Metaobject | **Hybrid (chosen)** |
|---|---|---|---|---|
| Dedicated SEO URL | No | Yes | Yes (dynamic page) | Yes |
| Native Search & Discovery filter | Yes, automatic | Yes, automatic | No (needs pairing) | Yes, automatic |
| Rich content (logo, story, banner) | No | Limited | Yes | Yes |
| Admin/Collections list clutter | None | +129 entries | None | +129 entries |
| Build effort | ~0 (already populated) | Moderate (bulk CSV) | High (schema + template) | Highest, phased |
| Collision risk with categories | None (no URL) | Yes (shared namespace) | None (separate namespace) | Yes (shared namespace, same as Collection) |

**Decision**: Build all three layers, phased:
1. **Vendor** field — already populated by `csv_generator.py`; zero
   additional work, gives automatic Search & Discovery filtering immediately.
2. **Smart Collection per brand** (rule: vendor = X), `/collections/{slug}`
   — this is what actually replaces the WooCommerce `/brand/{slug}/` pages
   from the Phase 5 redirect matrix and keeps their SEO equity. Built via
   bulk collection CSV in Phase 9, not now.
3. **Brand metaobject** (logo, story, hero banner, SEO fields), referenced
   from products and from its paired collection's template — deferred to a
   fast-follow after Phase 7 theme work exists to render it. Additive and
   non-breaking: doesn't change any URL or filtering behavior already live,
   so there's no cost to sequencing it after go-live if timeline pressure
   demands it.

Pure Vendor-only (Option A) was rejected because it silently drops 129
indexed, presumably-backlinked brand pages with no replacement — a real SEO
regression for a wholesale reseller where brand-name search intent
("NYX wholesale UK") is a plausible meaningful traffic source, and the
project's stated objective is preserving SEO equity, not just data. Pure
Metaobject (Option C) was rejected as the sole mechanism because it doesn't
natively drive Search & Discovery filtering without a paired Vendor/
Collection anyway — so "just metaobjects" isn't actually simpler, it's the
same eventual shape with the filtering layer missing.

**Consequences**: Collections list will hold ~26 category collections +
~129 brand collections + a handful of manual promo collections (see
ADR-007) — needs the naming convention in ADR-008 to stay collision-free.
Metaobject work is real scope that must be tracked, not silently dropped —
tracked as its own line in `docs/MIGRATION_PROGRESS.md`.

## ADR-007: Reclassify promo-bundle "brands" instead of routing around the collision

**Context**: The Phase 5 slug collision (`valentine-combo-deals` used by
both a `product_cat` and a `pwb-brand` term) was investigated at the data
level, not just the URL level. All 8 products tagged with the
"VALENTINE COMBO DEALS" brand are seasonal gift-set bundles (e.g.
"SKINCARE ESSENTIALS COMBO DEAL - VALENTINE COMBO DEALS") — not products
from a manufacturer called Valentine. A second entry, "TRADEFAIR COMBO
DEALS", has the same pattern (no products from an actual "Tradefair" brand).
Both are marketing/merchandising groupings that were tagged into the brand
taxonomy in WooCommerce as a workaround, most likely just to get them to
show up in the site's brand-filtered navigation.

**Decision**: Don't solve this with a handle-suffix convention (e.g.
`valentine-combo-deals-brand`). Fix the actual data: these entries are not
brands. They become **manual, hand-curated Shopify Collections** (product
picked individually, not rule-based), the same as any seasonal campaign
collection, unrelated to the Vendor/brand-collection system in ADR-006.
Their `pwb-brand` tagging is dropped; if the underlying products have a
real manufacturer, that becomes their Vendor value instead (needs a
manual check per product — flagged in the risk register, not automated,
since guessing a manufacturer from a bundle listing risks being wrong).

**Consequences**: Eliminates the only known collision without a naming
workaround. Establishes precedent: before building around a data conflict,
check whether the conflict is actually a data-quality problem. Any brand
term matching `combo|bundle|deal|tradefair|valentine|set` should get the
same manual review before Phase 9 collection import — full list is a
2-entry check, not a recurring process, at current catalog size.

## ADR-008: Handle naming convention for the Collections namespace

**Context**: ADR-006 puts category and brand collections in the same
`/collections/` namespace. ADR-007 removes the one known collision, but the
namespace is shared going forward, so new categories or new brands could
collide again as the catalog grows.

**Decision**: No blanket prefix/suffix is applied to all 129 brand handles
— `/collections/nyx` reads better and preserves more brand-name SEO value
than `/collections/brand-nyx` would, and a blanket prefix is unnecessary
cost paid by every brand to guard against a collision that, per ADR-007,
mostly doesn't legitimately occur. Instead: handles are always the plain
WooCommerce slug (lowercase, hyphenated, already how `seo_url_mapper.py`
and `csv_generator.py` produce them). If a genuine future collision arises
(a real brand name that's also a real category name), resolve it case by
case with a new ADR entry recording which side got the disambiguating
suffix and why — do not pre-emptively suffix the whole brand set for a
problem that, once ADR-007's cleanup is applied, doesn't exist in the
current catalog.

**Consequences**: Requires a slug-collision check as a standing step before
each future collection-creation run (already automated —
`seo_url_mapper.py`'s `write_duplicate_report` already performs this check
and should be re-run before Phase 9's bulk collection import, not just once
in Phase 5).

## ADR-009: WooCommerce's 3-level category tree does not become 3 levels of Shopify collections

**Context**: WooCommerce's `product_cat` taxonomy has real depth: 4
legitimate top-level groups (Makeup, Skin Care, Bath & Body Care, Beauty
Tools), a second level (e.g. Face, Eyes, Lips under Makeup), and a third
level under that (e.g. Foundation, Concealer under Face). Shopify collections
are flat — there's no native parent/child relationship between them.

**Decision**:
- **Level 1** (4 groups) becomes navigation structure only — top-level menu
  items with dropdowns — not collections themselves.
- **Level 2** (~26 categories) becomes real Shopify Collections — this is
  the actual browse/landing-page layer and matches what's in the Phase 5
  redirect matrix as the pages worth preserving.
- **Level 3** (sub-categories like Foundation, Concealer, Eyeliner) does
  **not** become a further layer of collections. It becomes each product's
  **Product Type** value, filterable via Search & Discovery *within* its
  Level 2 collection page. This mirrors the WooCommerce browsing experience
  (narrow from Face → Foundation) without a 47-collection sprawl that would
  be tedious to maintain and duplicate what filtering already does natively.

**Consequences**: `csv_generator.py`'s current `Type` column logic (first
category in list order) needs to change to "most specific assigned
category" once this feeds into Phase 9's product CSV — noted in
`docs/RISK_REGISTER.md`, not fixed now since Phase 6 is architecture-only
and the instruction for this phase was explicitly not to touch import
scripts. Also requires a one-time content cleanup: ~12 categories WooCommerce
has sitting at top level that are actually Level 2/3 content miscategorized
as Level 1 (e.g. "Eye cream", "Body Butter", "sponge") — mapping table in
`docs/SHOPIFY_ARCHITECTURE.md`.

## ADR-010: Markets / multi-currency / B2B — Business Decision Required

**Status: RESOLVED for Phase 9 scope (2026-08-10), via ADR-011 Decision 2.
The project owner explicitly deferred Markets/multi-currency/B2B: Phase 9's
initial migration is scoped to the UK storefront only. This is a real
decision — "explicitly deferred," not "still undecided" — recorded here
so this entry stops reading as an open question for Phase 9's purposes.
The underlying business question (whether Markets/B2B is ever wanted) is
still not answered and remains genuinely open for any future phase that
would need it; nothing below should be read as answering it.**

**Original context, preserved as historical record:**

**Context**: Phase 7's instructions require the theme architecture to
support Shopify Markets, multi-currency, and "future B2B support." No prior
phase — not the original migration scope, not any ADR, not a conversation
with the store owner — has actually specified:
- Which countries/regions the store should sell into as distinct Markets
- Which currencies should be offered, and whether pricing should vary by
  market or convert automatically
- Whether B2B (company accounts, net payment terms, quantity-break
  pricing, a separate wholesale catalog/price list) is actually wanted, or
  whether "Wholesale Beauty Hub" is a consumer-facing brand name only

That last point matters most: the store's name is the only signal
suggesting B2B might be relevant, and a name is not a requirement.

**What functionality is affected**: Company account switcher UI, net-terms
display and invoicing language, quantity-break/tiered pricing display,
market-specific pricing or currency-conversion behavior, and any
market-specific content (language, legal pages, shipping messaging) that
would differ by region.

**Which theme components depend on it**: None *require* it to function
today — `snippets/price.liquid` uses Shopify's `money` filter (currency-
format-agnostic by construction), and no component assumes a single
market or consumer-only audience (see `docs/THEME_ARCHITECTURE.md` §
Markets & B2B readiness). But several components would need real
(non-speculative) rework once this is decided: the account area
(`templates/customers/*`, `snippets/account-nav.liquid`) would need a
company/location switcher for B2B; `snippets/price.liquid` would need
tiered-pricing display logic; navigation and footer content might need
market-specific variants.

**Does it block theme development (Phase 7)?** No. Phase 7 is complete
without it — see above, nothing built assumes an answer either way.

**Does it affect Phase 8 (Media Migration)?** No. Media migration is
market/audience-agnostic.

**Does it affect Phase 9 (Product & Content Import)?** Potentially, but
not in a way that blocks starting Phase 9. If B2B is confirmed *wanted*,
Phase 9's product import may need wholesale-specific price lists or
customer tagging set up alongside the standard import. If Markets/multi-
currency is confirmed, Phase 9's collection/product setup is unaffected
(Markets configuration is a separate Admin settings layer on top of
existing products, not a re-import). Recommend deciding before Phase 9
product import begins so pricing setup only happens once, not deciding
before Phase 9 can start.

**Can it safely be deferred?** Yes, through Phase 8 and the start of
Phase 9. It becomes a real blocker only if Phase 9's pricing/customer
import needs a wholesale price-list structure that isn't yet decided, or
if Phase 12 testing is expected to cover B2B checkout flows that don't
exist. Recommend forcing the decision no later than before Phase 9's
"Import products" issue begins, not before Phase 8.

**Decision**: For Phase 9, deferred — UK storefront only, no Markets, no
multi-currency, no B2B (ADR-011 Decision 2). Do not implement Markets
configuration, currency conversion, or B2B account/pricing UI without a
separate, explicit answer from whoever owns this business call — deferring
is not the same as deciding "no," and this should not be read as
foreclosing Markets/B2B permanently. `docs/THEME_ARCHITECTURE.md` §
Markets & B2B readiness remains accurate as-is (theme is config-ready
without assuming an answer either way) and needs no change for this
resolution.

---

## ADR-011: Phase 9 test-import approval — Admin API, UK-only scope, approved test set and store

**Status: DECIDED (2026-08-10), by the project owner.**

**Context**: `docs/PHASE9_ENVIRONMENT_READINESS.md`'s human approval gate
required explicit sign-off on five things beyond technical readiness
before any real write to Shopify: import method, migration scope
(Markets/B2B), which store, which test products, and explicit authorization
to write. All five were provided directly by the project owner, not
inferred from credentials working or from prior recommendations.

**Decisions**:

1. **Import method**: Admin API/GraphQL, per the existing recommendation
   in `docs/PHASE9_IMPORT_STRATEGY.md`. Not to be reverted to CSV without
   a documented technical blocker. The import client must remain
   deterministic, idempotent, checkpointed, retry-safe, duplicate-safe,
   auditable, and reversible where technically possible — this document
   doesn't restate the mechanics, see `docs/PHASE9_ENVIRONMENT_READINESS.md`
   § Import safety design.
2. **Initial migration scope**: UK storefront only. Multi-country Markets,
   multi-currency, B2B, international pricing, and additional storefronts
   are explicitly out of scope for Phase 9 and deferred as future
   architecture work (see ADR-010's updated status above).
3. **Test environment**: `wholesale-beautyhub.myshopify.com`, isolated
   from production. No production store write is authorized by this
   decision.
4. **Test import set**: the existing 9-product set in
   `reports/phase9_test_import_set.csv`, traceable to real WooCommerce
   product IDs (`docs/PHASE9_ENVIRONMENT_READINESS.md` § Test import
   product set). Not to be replaced with a newly invented sample.
5. **Controlled test import**: authorized for exactly those 9 products,
   into the store named above only. Does **not** authorize the remaining
   602 products, any customer, any order, any production write, DNS/domain
   changes, production redirects, WooCommerce shutdown, or WooCommerce
   data deletion. A separate, explicit decision is required before any of
   those.

**Effect on the human approval gate**: 7 of 8 items now met. The one
remaining open item, production Shopify plan tier (§ G), is not required
by these decisions — it gates a future production import, not this test
import, which explicitly targets a development-store plan already in
place. Full detail and real (not simulated) execution evidence:
`docs/PHASE9_ENVIRONMENT_READINESS.md`, `reports/phase9_test_import_result.json`.
