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
