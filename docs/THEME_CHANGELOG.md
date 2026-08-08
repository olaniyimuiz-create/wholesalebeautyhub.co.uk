# Theme Changelog

Theme-code-specific history, kept separate from
`docs/MIGRATION_PROGRESS.md` (whole-project changelog) since this one
tracks `shopify/theme/` in more implementation detail than the
project-level log needs. Newest first.

## 2026-08-08 — Phase 7: initial theme build

Added `shopify/theme/`: a complete Online Store 2.0 theme foundation
(72 files) built against the approved architecture (`docs/SHOPIFY_ARCHITECTURE.md`,
`docs/SHOPIFY_FOUNDATION.md`) and this phase's explicit requirements.
No theme was previously deployed or committed — this is the first version.

**Structure**: JSON templates throughout (except `customers/*`, which
Shopify still serves as classic Liquid); 2 section groups
(`header-group`, `footer-group`) for dynamic header/announcement-bar
composition; 2 native Theme Blocks (`faq-item`, `trust-badge`); design
tokens as CSS custom properties mirrored into `settings_schema.json`.

**Commerce**: full product page (gallery, variant picker, buy box,
brand metaobject block with graceful fallback, bundle "included items"
block, sticky add-to-cart), full collection page (native Search &
Discovery filters and sort, no app), Ajax cart drawer + full cart page,
native predictive search, native product recommendations.

**Templates for the approved architecture**: `product.combo-deal.json`
(ADR — bundle products get an "included items" block, no separate
variant/collection logic needed beyond that), `collection.brand.json`
(brand metaobject-sourced hero, ADR-006 phasing — works whether or not
metaobject entries exist yet), `collection.promo.json` (manual promo
collections, ADR-007 — no Vendor/Product Type filters, since those don't
apply to hand-curated bundle collections).

**Conflicts found and resolved**: WCAG 2.1→2.2 AA version bump (see
`docs/THEME_ARCHITECTURE.md` § Conflicts); Markets/B2B scope was
undecided, resolved by building code-level readiness without
market-specific configuration or B2B UI (same doc).

**Verified before commit** (see `docs/PHASE7_REPORT.md` § Validation
Report for full detail): every `{% schema %}` block is valid JSON, every
`t:`/`| t` locale reference resolves against `locales/en.default*.json`,
every `render`/section `type` reference resolves to a real file. Two
correctness bugs caught and fixed during review: hand-built JSON-LD via
string concatenation in the breadcrumb schema (would have broken on
titles containing quote characters — rewritten to use the `json` filter
per field) and the same issue in the Organization schema's social links
array.

**Explicitly not built this phase** (see `docs/COMPONENT_LIBRARY.md` for
the full list and reasoning): Quick View, a full Quick Add flyout beyond
the trigger button, Testimonials (no source content exists), true color
swatches (this catalog's shade data is names like "Cape Town 05", not
colors — needs a data decision, not a theme one), and most customer
account sub-pages beyond login/register/order history.

**Explicitly not done this phase, by instruction**: no product/customer/
order import, no deployment, no live Theme Check or Lighthouse/axe run
(no deployed store exists to run them against — Shopify CLI installation
was attempted and blocked by a Windows admin-elevation prompt this
environment couldn't click through non-interactively; noted as an
environment gap, not skipped by choice).
