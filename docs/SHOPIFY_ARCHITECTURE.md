# Shopify Information Architecture (Phase 6)

Architecture blueprint only — no theme, no CSVs, no import scripts, no
production data touched. Design decisions are recorded as ADRs in
[docs/DECISIONS.md](DECISIONS.md) (ADR-006 through ADR-009); this document
is the detailed blueprint those decisions produce, grounded in the actual
WooCommerce data (`migration/sql/dump.sql`) and the Phase 5 findings in
[docs/SEO_STRATEGY.md](SEO_STRATEGY.md).

## Brand architecture

Full option comparison (criteria: Shopify best practices, SEO impact,
scalability, maintenance, Search & Discovery compatibility, filtering,
navigation, merchant usability, future expansion, impact on the existing
WooCommerce taxonomy):

| Criterion | A: Vendor only | B: Smart Collection | C: Metaobject | **D: Hybrid (chosen)** |
|---|---|---|---|---|
| Shopify best practice fit | Common for simple catalogs | Long-standing standard pattern | Modern, promoted for rich content hubs | Matches how larger Plus stores are actually built |
| SEO impact | **Negative** — no dedicated indexable brand URL/meta, loses the 129 currently-indexed `/brand/{slug}/` pages | Strong — direct URL/meta continuity with WooCommerce | Strong, plus richer on-page content signals | Strong (collection URL) + richest content (metaobject) |
| Scalability (129 → hundreds of brands) | Effortless | Linear admin/import work, but scriptable | Linear setup work, scriptable via Admin API | Linear, same scriptable work as B, metaobject layer optional per-brand |
| Ease of maintenance | Trivial | Low — one rule-based collection, self-updating as vendor tags change | Low per entry, but a second system to keep in sync with products | Slightly higher — three concepts, but only one is a true source of truth (metaobject); Vendor is auto-derived, Collection is a thin rule |
| Search & Discovery compatibility | Native, automatic | Native, automatic | **Not native** — needs a paired Vendor/Collection anyway | Native, automatic (via Vendor + Collection) |
| Filtering | Works everywhere automatically | Works on collection pages | Doesn't drive storefront filtering by itself | Works everywhere |
| Navigation | Needs custom build (no native brand index page) | Drops straight into nav as normal collection links | Needs custom nav wiring to metaobject page URLs | Drops into nav like B; metaobject content enriches it later |
| Merchant usability | Simplest (already just a text field) | Familiar — merchants already manage collections | Less familiar content model, more setup per brand | Familiar entry point (collections), richer editing available later |
| Future expansion | Limited — plain string, no room for brand content | Moderate — collection image/description only | High — structured fields, custom templates | Highest — has the room without being forced to build it all now |
| Impact on WooCommerce taxonomy | Fully preserves Vendor mapping, drops brand pages entirely | 1:1 replacement of `pwb-brand` archive pages | Requires reshaping brand data into a new schema | Preserves the pages (via Collection) and gives the taxonomy room to grow richer over time |

**Decision: Option D, phased.** Vendor field and per-brand Smart Collections
are the Phase 9 deliverable (they directly replace what's in the Phase 5
redirect matrix). The brand metaobject content layer is scoped but
deliberately sequenced *after* Phase 7 theme work exists to render it —
see ADR-006 for the full reasoning, including why A and pure-C were
rejected.

### Data-quality finding that resolves the actual blocking collision

The `valentine-combo-deals` collision isn't a naming problem — it's 8
products that are Valentine's Day gift bundles, tagged into the brand
taxonomy as a workaround, not products from an actual brand called
"Valentine." Same pattern found in "TRADEFAIR COMBO DEALS" (no matching
category collision today, but same root cause). See ADR-007: both become
manual, hand-curated Collections — a normal Shopify pattern for seasonal
merchandising — and are removed from the brand/Vendor system entirely.
This is the actual fix; no handle-suffix workaround is needed (ADR-008).

## Category hierarchy

WooCommerce has real 3-level depth: 4 top-level groups → ~26 second-level
categories → sub-categories under those. Shopify collections have no
native parent/child relationship, so the tree doesn't map 1:1 (ADR-009):

```
Level 1 (nav structure only, not collections)
├─ Makeup
│   ├─ Level 2 → Collection: Eyes, Face, Lips, Brushes & Beauty Blenders
│   │   └─ Level 3 → Product Type (filter, not a collection):
│   │       Face: Foundation, Concealer, Blushes, Bronzers & Highlighters,
│   │              Highlight & Contour, Illuminators, Powder,
│   │              Setting & Finishing Spray, Primers
│   │       Eyes: Eyeliner, Eye Pencil, Eyebrow Products,
│   │              Eyelash Extensions & Glue, Eyeshadow, Mascara,
│   │              Pigment/Glitter & Base
│   │       Lips: Lip Gloss, Lip Pallet, Lip Pencil, Lipbalm, Lipstain,
│   │              Lipstick, Lip oil
├─ Skin Care
│   └─ Level 2 → Collection: Serums & Treatment, Moisturizers & Cream,
│       Face Cleansers & Wash, Face Toners/Mist & Essence,
│       Exfoliators/Peels & Scrubs, Sunscreen, Mask, Face Oil
├─ Bath & Body Care
│   └─ Level 2 → Collection: Body Moisturizers, Body Scrubs, Body Wash,
│       Body Lotion, Body Oil, Roll On, Cotton Pad*, Hand cream
├─ Beauty Tools
│   └─ Level 2 → Collection: Tools & Accessories
```
\* Cotton Pad and Illuminators currently have 0 published products — skip
creating these collections until they're populated, to avoid empty pages.

### Content cleanup needed before Phase 9 (recommendation, needs a human sign-off)

17 categories currently sit at the top level in WooCommerce, but only 4 are
real top-level groups. The rest are either promo groupings (handled above)
or content that's actually Level 2/3 and got left unparented:

| Stray top-level category | Recommended new home | Why |
|---|---|---|
| acne treatment | Skin Care › Face Cleansers & Wash (or its own Level 2) | Skin concern, not a top-level group |
| Glow serum, Dark spots & Discoloration serums, Eye cream, Glow spray | Skin Care › Serums & Treatment | Overlaps an existing Level 2 category; likely mergeable rather than kept separate |
| Body Body, Body Butter, Body Mist | Bath & Body Care | Matches existing Body Moisturizers/Lotion/Oil siblings |
| sponge | Beauty Tools › Tools & Accessories | Applicator, not a product family |
| Baby wash & lotion | Bath & Body Care | Thematically bath/body, not its own group |
| Uncategorized | (none — WordPress default bucket) | Audit for any product relying on it as primary category before dropping |
| TRADEFAIR COMBO DEAL, Combo Deals, VALENTINE COMBO DEALS\* | Manual promo collections | Not organic taxonomy — see ADR-007 |

\*Valentine's *category* term already nests correctly under Bath & Body
Care today — only its *brand* tagging is the problem (ADR-007).

This table is a starting recommendation, not a final content decision —
confirm with whoever owns merchandising before Phase 9 collection creation.

## Product Types

One value per product, set to the **most specific** WooCommerce category
assigned (the Level 3 value in the tree above — e.g. "Foundation", not
"Makeup" or "Face"). Products with no Level 3 category keep their Level 2
value (e.g. bundle/combo products → "Bath & Body Care" or similar). This
is the field Search & Discovery uses to filter within a Level 2 collection.

**Known gap to fix in Phase 9, not now**: `csv_generator.py`'s current
`Type` column uses `categories[0]` (first category in whatever order the
term relationships were stored), not the most specific one. Flagged in
`docs/RISK_REGISTER.md` — this phase is architecture-only, so the script
isn't touched here.

## Vendors

Populated directly from the `pwb-brand` taxonomy, already how
`csv_generator.py` sets the `Vendor` CSV column. 129 of 166 brand terms
have at least one published product; the other 37 are skipped automatically
since nothing references them. The two promo-bundle entries (ADR-007) are
excluded from Vendor entirely — those 8 products' Vendor value needs a
manual per-product check for a real manufacturer (or left blank), not an
automated guess.

## Tags

WooCommerce's 103 `product_tag` terms map directly to Shopify product tags
— free-text labels, no structural change needed. Used for secondary Search
& Discovery filtering (e.g. ingredient/concern tags like "Hyaluronic",
"Retinol", "Hydrating") and internal search relevance. Per Phase 5's
ADR-003, tags do **not** get a dedicated collection/URL — that's what the
Level 2 category collections and Product Type filter are for.

## Metafields

| Metafield | Type | Purpose |
|---|---|---|
| `custom.brand` | Metaobject reference (→ `brand` metaobject) | Links a product to its rich brand content, once the metaobject layer is built |
| `custom.included_items` | List of single-line text | Populated only for combo/bundle products — powers the "what's included" block on `product.combo-deal.json` |

No other structured product attributes were found in the WooCommerce data
worth a dedicated metafield (ingredient lists, skin-type suitability, etc.
exist only as unstructured text inside product descriptions today — future
enhancement, not a migration blocker).

## Metaobjects

`brand` metaobject definition (fast-follow per ADR-006, not built this
phase):

| Field | Type |
|---|---|
| `name` | Single line text |
| `handle` | (metaobject's native handle — must match the paired collection's handle exactly) |
| `logo` | File (image) |
| `hero_image` | File (image) |
| `description` | Rich text |
| `founded_year` | Integer (optional) |
| `website` | URL (optional) |
| `seo_title` / `seo_description` | Single line text |

Referenced from products via `custom.brand`, and from the brand collection's
own template (`collection.brand.json`) to source its hero/description —
single source of truth, the collection doesn't duplicate the content.

## Search & Discovery filters

Standard filter set per collection page: **Vendor** (brand), **Price**,
**Availability**, **Product Type** (the Level 3 value), and select **Tags**
for concern/ingredient filtering where tag coverage is consistent enough to
be useful (needs a quick tag-quality pass in Phase 9 — some tag names are
inconsistent casing/duplicates per the Phase 5 findings, e.g. "MASCARA" vs
lowercase tags elsewhere).

## URL conventions & handle naming standards

| Content type | URL | Handle source |
|---|---|---|
| Product | `/products/{handle}` | WooCommerce `post_name`, unchanged (already how the Phase 3/4 pipeline works) |
| Category collection | `/collections/{handle}` | WooCommerce category slug, unchanged |
| Brand collection | `/collections/{handle}` | WooCommerce brand slug, unchanged (ADR-008 — no prefix/suffix by default) |
| Manual promo collection | `/collections/{handle}` | Chosen at creation time, matching the WooCommerce category slug where one already exists (e.g. `valentine-combo-deals`) |
| Page | `/pages/{handle}` | WooCommerce page slug, unchanged (Phase 5) |
| Blog post | `/blogs/news/{handle}` | WooCommerce post slug, unchanged (Phase 5, ADR-004) |
| Brand metaobject page (fast-follow) | TBD at Phase 7 setup — Shopify's dynamic metaobject page URL prefix is configured in Online Store settings, not asserted here without confirming in Admin | Same handle as the paired brand collection |

Collision rule (ADR-008): plain slugs everywhere, no blanket disambiguation
prefix. Re-run `migration/scripts/seo_url_mapper.py`'s duplicate-URL check
before every future bulk collection import, not just once in Phase 5.

## Navigation

**Header** (matches the current site's structure, which the Phase 5 nav
analysis showed is a single mega-menu — Home, Brands, Makeup, Skincare,
Beauty Tools):

```
Home | Makeup ▾ | Skin Care ▾ | Bath & Body Care ▾ | Beauty Tools ▾ | Brands ▾
```
Each dropdown lists its Level 2 collections. "Brands" lists a curated
subset (top/featured brands — 129 is too many for a dropdown) plus a
"View all brands" link to a brand index page.

**Footer** — rebuilt as an actual Shopify navigation menu this time, not
theme-hardcoded content. Phase 5 found the current WooCommerce footer
(About, Privacy Policy, Refund & Returns, Terms & Conditions, Shipping
Policy) isn't in any `wp_nav_menu` — that's exactly the blind spot to fix:
```
About | Contact | Privacy Policy | Terms & Conditions | Shipping Policy |
Refund & Returns Policy | Cookie Policy
```
"Contact" and "Cookie Policy" were flagged in Phase 5 as not linked from
anywhere discoverable on the live site — include them here regardless, and
treat their absence from the current footer as a defect being fixed, not a
pattern to preserve.

## Collection templates

- **`collection.json`** (default) — category collections. Search &
  Discovery filters enabled (Vendor, Price, Availability, Product Type,
  select Tags).
- **`collection.brand.json`** — brand collections. Same filters minus
  Vendor (redundant on a single-vendor page); hero/logo/description sourced
  from the paired `brand` metaobject once that layer exists, falls back to
  the plain collection image/description until then.
- **`collection.promo.json`** — manual campaign collections (Valentine,
  Tradefair, seasonal). Campaign-style hero banner, no Product Type filter
  (bundles don't share a consistent type), no Vendor filter.

## Product templates

- **`product.json`** (default) — all standard products, simple and
  variable alike (Shopify variants handle both natively — see
  `docs/ARCHITECTURE.md` for how `database_parser.py`/`csv_generator.py`
  already produce these).
- **`product.combo-deal.json`** — bundle/combo products (the ~10 products
  currently under the promo categories). Adds an "In this bundle" block
  driven by the `custom.included_items` metafield.

## Brand pages

Primary, functional brand page: `/collections/{brand-slug}` (Smart
Collection, rule: Vendor = brand name) — live from Phase 9, replaces the
WooCommerce `/brand/{slug}/` archive with full SEO/URL continuity per the
Phase 5 redirect matrix. Richer content (logo, story, hero banner) layers
in via the `collection.brand.json` template once the metaobject exists —
additive, doesn't require a second page or a second URL.

## What's deliberately not decided here

- Exact Shopify Admin configuration steps for dynamic metaobject page URLs
  — confirm in Admin during Phase 7, not asserted from outside the platform.
- Which specific brands get featured in the header "Brands" dropdown —
  merchandising decision, not an architecture one.
- Whether "Contact" gets a dedicated page or a contact form app — Phase 7
  scope.
