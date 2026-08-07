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
