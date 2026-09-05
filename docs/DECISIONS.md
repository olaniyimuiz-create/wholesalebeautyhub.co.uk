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

## ADR-012: Broken WooCommerce variation handling — skip the variation, not the parent product (Option A)

**Status: DECIDED (2026-08-10), by the project owner, during Phase 9.7
Step 5 (controlled bulk import) execution.**

**Context**: The pilot batch of the 598-product bulk import crashed with
`IndexError` on product 18. Root cause, confirmed against raw
`wp_postmeta`: variation post 10966 (a real, `publish`-status WooCommerce
variation belonging to product 18) has `attribute_pa_shade = ''` — a
literal empty string, not a parsing artifact. A full-catalog scan found
exactly one other case: variation 19990 (product 16464). Both variations
have no SKU, price, or stock either — genuinely incomplete/broken records,
not something recoverable from other fields.

**Decision**: **Option A.** Skip only the broken variation; import the
parent product with its remaining valid variations. Do not quarantine the
whole product for one broken variation among many valid ones (product 18:
9 of 10 valid; product 16464: 22 of 23 valid). Do not invent a value for
the missing attribute. Every skipped variation is logged with an explicit
audit record (`reports/phase9_skipped_variations.jsonl`): WooCommerce
product/variation ID, classification `BROKEN_VARIATION_SKIPPED`, run ID,
importer commit.

**Implementation**: `is_valid_variation()`/`partition_variations()` in
`migration/scripts/phase9_dry_run.py`, applied before any Shopify
`productOptionsCreate`/`productVariantsBulkUpdate` mutation in
`migration/scripts/phase9_test_import.py`. Verified live: product 18 and
16464 both reconcile with 0 mismatches, correct variant counts (9 and 22),
and the broken variation is provably absent from the live product (no
placeholder, no fabricated Shade value).

## ADR-013: Missing-price handling — never fabricate £0.00 (Option A for partial, quarantine for total)

**Status: DECIDED (2026-08-10), by the project owner, during Phase 9.7
Step 5 (controlled bulk import) execution.**

**Context**: Live reconciliation after a tiered batch found product 69
("Msmetics 14In1 Lash Set") had 4 of 11 variants created at a fabricated
Shopify price of £0.00. Root cause: `regular_price` and `price` are both
genuinely empty for those 4 WooCommerce variations (confirmed against
`migration/data/products.json`); the importer's pre-existing pricing code
defaulted to `'0.00'` whenever no price was found
(`str(price) if price else '0.00'`), silently converting "price unknown"
into "free." Product 69 is itself one of the site owner's own
pre-migration `PRICE_INTEGRITY_FLAGGED_IDS` (risk #24) — corroborating
evidence this was a real, pre-existing data problem, not new. A
full-catalog scan found the complete scope: product 69 (partial — 4 of 11
variants unpriced) plus 10 wholly unpriced `draft` simple products
(25089, 25092, 25109, 25111, 25113, 25115, 25117, 25217, 25219, 25369).

**Decision**:
1. **Product 69 (partial pricing)**: Option A, same precedent as ADR-012.
   Import the parent with its 7 genuinely priced variants; skip the 4
   unpriced ones (classification `MISSING_PRICE_SKIPPED`, reason
   `NO_SOURCE_PRICE`). Do not fabricate a price. Do not copy another
   variant's or the parent's price.
2. **The 10 wholly unpriced products**: **quarantine entirely**
   (`no_source_price`) — there is nothing sellable to create. Not
   imported, not created in Shopify, no £0.00 assigned.
3. **Live correction**: the 4 already-live £0.00 variants on product 69
   (created before this decision) were removed via the minimal,
   schema-verified `productVariantsBulkDelete(productId, variantsIds)`
   mutation — the parent product and its 7 legitimate variants were not
   touched. Verified independently: product 69 now has exactly 7
   variants, all with their real source prices, £0.00 nowhere.

**Implementation**: `has_price()`/`partition_by_price()`/
`flag_missing_price()` in `migration/scripts/phase9_dry_run.py`. The
importer's price-defaulting fallback (`'0.00'`) was removed entirely;
`set_simple_variant()` now raises rather than silently fabricating a
price if this state is ever reached (defense-in-depth — a simple product
with no price is quarantined upstream and should never reach it).
Regression-tested: `migration/scripts/test_phase9_pricing.py`, 8
scenarios / 13 assertions, all passing. Full-catalog verification: 0
fabricated £0.00 prices remain anywhere in the store; 0 legitimate
(source-backed) £0.00 prices exist in the catalog at all.

## ADR-014: Phase 10 customer import — business decisions required

**Status: PARTIALLY SIGNED (2026-08-22). Gates 3 and 6 decided; Gates 1, 2, 4, 5 and 7 remain open.**

This records what is being asked and provides the form in which an answer is
recorded. It is not an answer. Every `Decision:` field below is deliberately
blank, and a blank field means the gate is open.

**Nothing in this ADR authorizes a Shopify write of any kind.**

### What has changed since this ADR was first written

Three of the original five items are resolved, and the prerequisite is cleared:

* **Item 1, import method — RESOLVED.** Admin GraphQL API, specifically
  `customerCreate` + `customerAddressCreate`, ratified 2026-08-21 (see
  `docs/PHASE10_CUSTOMER_SET_DECISION.md`). `customerSet` is out of scope.
* **Item 3, shipping address — SUPERSEDED.** Now Gate 2 below, restated as a
  policy choice with measured call counts rather than a yes/no on schema work.
* **Prerequisite, customer scopes — CLEARED.** `read_customers` and
  `write_customers` are granted; 24 scopes verified live 2026-08-22.
* Also newly established, and not visible when this ADR was drafted: **517
  customers share a phone number** with someone else (Gate 1), and **247 carry
  a conflicting name** (Gate 5). Neither existed as a known issue before.

### Verified state at the time of signing

Reproduced byte-identically across runs; re-verify with
`python migration/scripts/phase10_preflight.py` before signing.

| | |
|---|---|
| Source rows | 13,043 |
| IMPORT-eligible | 12,096 |
| Duplicate SKIP / QUARANTINE / EXCLUDE | 407 / 539 / 1 |
| Live Shopify customers | 0 |
| Mutation cost | 10 points, measured, 0 records created |
| Technical pre-flight | 13 passed, 0 failed |

---

### GATE 1 — Phone collisions

**Question**: 240 groups covering 517 customers share a phone number. Shopify
enforces store-wide uniqueness, so the field cannot be sent for all of them.

**Prepared**: `reports/phase10_phone_collision_review.csv` (gitignored, 578
rows). Applying the approved ownership rule produced 76 `KEEP_ONE`, 155
`OMIT_FROM_ALL`, and 9 `MANUAL_REVIEW_REQUIRED`. Per customer: 76 send, 422
omit, 19 held. The 27-customer group is `OMIT_FROM_ALL` — all 27 are guest rows
with zero registered accounts.

**What is needed**: a verdict on the 9 contested groups, and confirmation or
override of the other 231. Unresolved groups default to omitting the phone; the
customer is still created.

```
Decision:      CONFIRMED — the 231 recommendations stand as issued; the 9
               contested groups omit the phone for every member
Decided by:    Project/store owner
Date:          2026-08-22
```

**Recorded scope**: **76** customers send their number, **441** omit it (422
recommended `OMIT`, plus the 19 held in the 9 contested groups). All 517 are
created in full — omitting a phone never omits a customer.

**No phone number is deleted or altered in WooCommerce.** The only question this
gate settled is whether a number is *sent* to Shopify.

**Deliberately left open, and not a blocker**: the 9 contested groups were not
adjudicated. Each has two or more members with genuine individual ownership
evidence, and the owner chose the safe default rather than a guess. Those 19
customers can be reviewed at any time and the number added afterwards with
`customerUpdate` — no re-import, no rework. Choosing wrong now would attach one
person's phone number to another person's record, which is the one outcome here
with a real-world cost.

---

### GATE 2 — Address policy

**Question**: billing only, or billing + shipping?

**Measured**: Option A = 4,713 address calls · Option B = 5,922. 4,730 customers
end with at least one address; 7,351 have none in source; 1,193 have both; 16
have shipping only.

**No recommendation is offered, deliberately.** The deciding question is whether
an unlabelled second address on 1,193 records helps or confuses staff — Shopify
draws no billing/shipping distinction, so both land in one list ordered by
`setAsDefault`. That is an operational judgment about how the team works, not a
technical one. The runtime supports either via `include_shipping`.

```
Decision (A or B):   A_PLUS — billing, falling back to shipping ONLY for a
                     customer who has no billing address
Decided by:          Project/store owner
Date:                2026-08-22
```

**A third shape, introduced at decision time.** A and B were not the only
options available, and the binary hid a real cost in each: option A would have
imported **17** customers with no address at all while usable address data sat
unused in the source, and option B would have given **1,192** customers a second
address that Shopify renders with no billing/shipping label. A_PLUS pays
neither price.

| | Calls | Customers with address data left addressless | Customers with an unlabelled second address |
|---|---:|---:|---:|
| A — billing only | 4,713 | **17** | 0 |
| B — billing + shipping | 5,922 | 0 | **1,192** |
| **A_PLUS — selected** | **4,730** | **0** | **0** |

Measured across all 12,096 by `phase10_address_readiness.py`, not estimated:
4,713 + 17 = 4,730 ✓. The cost over option A is **17 extra calls**.

**Implemented and tested**: `rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING`, selected
via `plan_addresses(policy=…)`. A policy string nothing implements raises
`UnknownAddressPolicy` rather than falling back to a real policy — importing
thousands of customers under a rule nobody chose is the failure this prevents.
Options A and B remain implemented, because a decision recorded in a document
is a decision that can be revised.

**One earlier figure corrected**: the narrative above says "1,193 have both; 16
have shipping only". Measured per customer, it is **1,192 both and 17
shipping-only**. The 4,713 / 5,922 call counts are unchanged and were always
right; only the per-customer split was off by one in each direction.

---

### GATE 3 — Marketing consent

**Question**: may FluentCRM's `subscribed` status be carried into Shopify's
`emailMarketingConsent`?

**Scope**: 6,295 `subscribed` records. The other three cases need no decision —
229 `unsubscribed` (honour the opt-out), 21 `pending` (double opt-in never
completed), 5,551 no signal (safe default).

**Current policy, unchanged**: `emailMarketingConsent` is omitted for **all
12,096**. The open question is legal, not technical: whether FluentCRM's
original opt-in mechanism is a sufficient basis under UK GDPR/PECR to carry
consent into a different platform. Nothing in the database export answers it.

**This is the one gate that is not the store owner's alone to close** — it needs
the data controller, or their advisor. The verbatim statement to sign or decline
is in `docs/PHASE10_GDPR_CONSENT.md` § 5.

```
Decision (approve / decline):  APPROVED — carry FluentCRM `subscribed` forward
Decided by (role):             Project/store owner
Date:                          2026-08-22
```

**Recorded scope of this approval**: `emailMarketingConsent.marketingState =
SUBSCRIBED` for the **6,295** FluentCRM `subscribed` records. The other three
cases are unchanged and were never in question: 229 `unsubscribed` remain
UNSUBSCRIBED, 21 `pending` are omitted (double opt-in never completed), 5,551
with no signal are omitted.

**Recorded limits of what was verified** — stated because a consent decision
should carry its evidentiary basis, not because it reopens the decision. This
pipeline has no visibility into how FluentCRM originally collected consent:
single vs. double opt-in, and what disclosure was shown, are not present in the
database export and were not established. The approval above is the owner's
judgment that the original opt-in is a valid basis under UK GDPR/PECR; it is not
a technical finding, and this project did not verify it.

**Not yet applied.** No customer carries consent, because no customer exists.
This governs the full import. The Gate 6 test cohort runs with consent omitted,
which is what Gate 6 authorized.

**Implementation note**: consent can also be applied after import via
`customerEmailMarketingConsentUpdate` without re-importing anyone, so this
decision does not block or gate the import order.

---

### GATE 4 — 292 missing-email records

**Question**: permanently exclude, or attempt recovery?

**Evidence**: all 292 are guest rows — no user account, no phone, no billing
address; 291 have only a name. The guest fallback reads `wp_wc_order_addresses`
**keyed by email**, so it cannot recover one. No other source in the parsed dump
carries an email for these rows.

**Recommendation**: `PERMANENT_EXCLUSION`. No email is fabricated or
synthesised. Their orders remain migratable separately as guest orders.

```
Decision:      PERMANENT_EXCLUSION — confirmed
Decided by:    Project/store owner
Date:          2026-08-22
```

**Recorded scope**: the 292 are excluded from the customer import permanently,
not deferred. No email is fabricated, synthesised, or derived. They are already
outside the 12,096 IMPORT population, so this confirms the existing
classification rather than changing any number.

**What this does not decide**: their orders. Those remain migratable separately
as guest orders, which is an order-migration question and not this gate's.

---

### GATE 5 — 247 conflicting-name records

**Question**: which name is correct, and what does an unresolved conflict do to
the run?

**Prepared**: `reports/phase10_name_conflict_review.csv` (gitignored, 247 rows,
every `chosen_name` empty). Reviewer writes `IMPORT_NAME`, `ALTERNATE_NAME`, or
`MANUAL_REVIEW`; an unrecognised token raises rather than being interpreted.
Triage: **94 genuinely ambiguous, 153 near-mechanical** — of which 16 would
otherwise import with no name at all and are worth doing first.

**Nothing is pre-selected and nothing can be.** WooCommerce records no ordering
or authority between the two variants, so there is no evidence to score.

**Policy recommendation**: `EXCLUDE_AFFECTED_CUSTOMERS` — import 11,849 now, the
247 once confirmed. Blocking all 12,096 is disproportionate; creating a customer
under an unconfirmed name is the option with a real-world cost.

```
Name decisions:   NOT REQUIRED under the selected policy — all 247 remain
                  unresolved in reports/phase10_name_conflict_review.csv
Policy decision:  EXCLUDE_AFFECTED_CUSTOMERS
Decided by:       Project/store owner
Date:             2026-08-22
```

**Recorded scope**: the bulk import population becomes **11,849**, not 12,096.
The 247 affected customers are held back, not dropped — they import unchanged
once a reviewer fills `chosen_name`, with no rework and nothing lost.

**Why the names could be left unresolved**: this policy means no customer is
ever created carrying a name no human has confirmed, so the 247 name decisions
stop being a precondition for the run. They remain genuinely owed to those
customers — 16 of them would otherwise import with no name at all — but they are
now follow-up work rather than a gate.

**Enforced, not assumed**: `name_conflict_gate()` raises if the policy is
`BLOCK_ENTIRE_MIGRATION` and conflicts remain, and removes the affected Woo IDs
from the population under `EXCLUDE_AFFECTED_CUSTOMERS`. An unrecognised
`chosen_name` token raises rather than being interpreted.

---

### GATE 6 — Test cohort authorization (Step 10)

**Separate from Gates 1–5 and not implied by any of them.**

**Question**: authorize a 10-customer test import into
`wholesale-beautyhub.myshopify.com` (development store, 0 customers) using
`reports/phase10_test_import_set.csv`?

**This creates real customer records containing real people's data.** It is
reversible via `customerDelete`, and the rollback window closes when the store
goes live.

**Prerequisite**: Gates 1–5 govern what is *sent*. A test import run before they
close would exercise the pipeline under provisional rules — which is a
legitimate thing to want, but it should be a deliberate choice rather than an
oversight. State explicitly whether the test may precede them.

```
Decision:                            AUTHORIZED — create, reconcile, then delete
May the test precede Gates 1–5:      yes
Decided by:                          Project/store owner
Date:                                2026-08-22
```

**Authorized scope, exactly**: the 10 customers in
`reports/phase10_test_import_set.csv`, into
`wholesale-beautyhub.myshopify.com` (development store), under these
provisional rules:

* `emailMarketingConsent` **omitted for all 10** — Gate 3's approval governs the
  full import, not this test
* **Option A addressing** (billing only) — 7 `customerAddressCreate` calls
* phone **omitted** for the 3 collision-affected customers
* name-conflict customers **excluded** from the cohort entirely
* `custom.legacy_woo_customer_id` on every record, inline

**Expected: 10 `customerCreate` + 7 `customerAddressCreate` + 10
`customerDelete` = 27 mutations.** The store returns to 0 customers.

**Not authorized by this gate**: any customer outside those 10, any second run,
and the full import (Gate 7).

### Gate 6 — EXECUTED 2026-08-22

| | |
|---|---|
| Cohort | 10 |
| Created | **9** |
| Failed | **1** (woo 1 — `Phone is invalid`, customer not created) |
| Address calls | 6 succeeded of 7 planned (the 7th belonged to the failed customer) |
| Deleted | 9 — all created records removed |
| Mutations | 25 (10 create, 6 address, 9 delete), 250 points |
| Store after | **0 customers**, verified by listing records, not by count alone |
| Consent set on any customer | **No** — as authorized |
| Reconciliation | 117 field checks, 4 apparent mismatches, **0 real** |

**The 4 mismatches were Shopify normalising UK phone numbers to E.164** —
planned `07…`, live `+44…`, same subscriber, confirmed digit-by-digit. The
reconciliation now compares canonical forms so a future run does not report a
false mismatch.

**The test earned its keep by failing.** `woo 1` was rejected outright on an
invalid phone and **no customer was created** — see risk register #45. The
design already specified retry-without-phone for exactly this case; the executor
did not implement it. That gap is now recorded and must be closed before Gate 7.

**Closed 2026-08-22.** `phase10_import_runtime.phone_fallback()` now decides the
retry and builds the payload — phone removed, customer tagged
`phone-dropped-invalid`, legacy metafield asserted through — and
`phase10_test_import.create_customer()` re-issues the create exactly once. The
runtime still cannot send a mutation; it decides and transforms, the executor
sends.

`phase10_phone_format_validator.py` sizes the exposure offline, without a single
request: of the **4,450** customers carrying a phone, **10** are structurally
invalid and **44** carry a GB national number of the wrong length — **54**
flagged, woo 1 among them. That is the expected fallback rate for the bulk run:
roughly 54 customers who keep their record and lose their number, not 54
customers lost.

The pre-check is structural and cannot certify that Shopify will accept a
number. woo 1 passes every generic length test and was still rejected, which is
precisely why the retry — not the pre-check — is what makes the bulk run safe.

**Idempotency confirmed**: the startup legacy-id scan ran, found 0 existing, and
every created customer carried `custom.legacy_woo_customer_id` verified against
the live response before proceeding. Deletion re-verified the legacy id on each
record before removing it.

---

### GATE 7 — Bulk import authorization

Separate again, for the full IMPORT-eligible set. **Not implied by Gate 6.**

**REQUESTED 2026-08-22.** Gates 1–6 are signed, every technical precondition
passes, and the run has been planned against the signed policies rather than
against the pre-decision figures. What follows is the request; the `Decision:`
field below is the only thing that grants it.

#### What is being asked for

Authorization to create **11,849** customers in
`wholesale-beautyhub.myshopify.com`, once, under the policies signed above.

| | |
|---|---|
| Customers created | **11,849** (12,096 IMPORT minus 247 held by Gate 5) |
| `customerCreate` | 11,849 |
| `customerAddressCreate` | 4,521 |
| **Total mutations** | **16,370** (16,421 worst case with phone fallbacks) |
| Cost | 163,700 points at the measured 10/mutation |
| Duration | **~27 minutes** at the measured sustained 10 mutations/s |
| Addresses | 4,521 customers get exactly one; none gets two |
| Phones sent | 3,799 · 428 omitted by Gate 1 · 51 expected to fall back |
| Metafields | 11,849 legacy ids · 6,581 also carry `woo_registered_at` |
| Consent | not applied by this run — 6,065 in-run `subscribed` records can be set afterwards |

Measured by `phase10_run_plan.py`, offline, from the source dump. It reproduces
the 240 collision groups and the 76/155/9 split exactly, which is what gives
confidence the rest of its arithmetic is reading the same population every other
report reads.

#### What authorizing this does NOT do

* **It does not make a run possible today.** No bulk importer exists.
  `phase10_test_import.py` is hard-capped at 10 records and
  `phase10_import_runtime.py` refuses mutation documents by construction. Gate 7
  authorizes a program that still has to be written, and that program should be
  reviewed before it is pointed at 11,849 customers.
* **It does not authorize a second run.** Resume after an interrupted run is
  supported through the legacy-id map, but a fresh full run is a fresh decision.
* **It does not authorize consent.** Gate 3 approved it; applying it is a
  separate `customerEmailMarketingConsentUpdate` pass.

#### What is known to go wrong, before it does

* **~51 customers will have their phone number dropped** (risk #45). Shopify
  rejects the whole `customerCreate` on a phone validation error, so the runtime
  drops the number, tags the customer `phone-dropped-invalid`, logs the original,
  and retries once. Nobody is lost; roughly 51 people arrive without a phone.
  That figure is a floor — the pre-check is structural, and Shopify validates
  against numbering plans this project does not hold.
* **19 customers keep no phone** pending the 9 contested collision groups.
* **247 customers do not import at all** until their names are confirmed.
* **15 customers import with no address**, their source address being
  unusable — no country, and GB is never assumed.

#### Rollback

`customerDelete` per record, driven by the ledger, within the window before the
store goes live. **Mass deletion is explicitly not the rollback mechanism** — see
`PHASE10_IMPORT_PROCEDURE.md` §11. After go-live, a created customer is a real
customer and deletion is a business decision, not a technical one.

#### Preconditions, all currently true

| | |
|---|---|
| Pre-flight | 15 passed, 0 failed, 0 gates open, exit 0 (2026-08-22) |
| Store | 0 customers, verified by listing records |
| Gates 1–6 | signed |
| Offline suite | 644 + 63 assertions passing |
| Test import | executed and reverted; 9 created, 1 lost to risk #45, now fixed |

```
Decision:
Decided by:
Date:
Store and date the run is authorized for:
```

---

**Not authorized by this ADR**: any Shopify customer write, of any kind, test or
bulk. A gate is closed only when its `Decision:` field is filled in and dated.
`phase10_preflight.py` reads the blocking column of
`docs/PHASE10_DECISION_MATRIX.md` and will not report READY while any gate
remains open.
