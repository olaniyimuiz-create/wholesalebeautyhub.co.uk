# Shopify Build Guidelines

How Phase 7 (theme development) and beyond should proceed, once approved.
Inputs to every build decision are `docs/SHOPIFY_ARCHITECTURE.md` (why) and
`docs/SHOPIFY_FOUNDATION.md` (what) — this document is about how to build
it without drifting from those.

## Foundation-first, not theme-first

Build in the dependency order in `docs/SHOPIFY_DEPLOYMENT.md` § Import
sequence. Don't write a collection template before the collection
architecture it renders exists in `shopify/foundation/collections.json`,
and don't hand-wave a metafield's shape in Liquid before it's defined in
`shopify/foundation/metafields.json`. If a build decision requires
deviating from the Foundation spec, update the spec first (and the ADR if
it's architectural, not just implementation detail) — the doc and the
build should never silently diverge.

## Online Store 2.0, section-first

Build with sections and blocks (JSON templates), not hardcoded Liquid
templates — every content area a merchant might reasonably want to
rearrange or re-theme later should be a section, not baked into
`product.json`/`collection.json` directly. This is also what makes the
`collection.brand.json` / `collection.promo.json` / `product.combo-deal.json`
alternate templates from `docs/SHOPIFY_ARCHITECTURE.md` practical — they're
compositions of the same section library, not one-off page builds.

## Work incrementally, matching how this migration has run so far

One deliverable at a time: build a section, verify it renders correctly
against real data (use the actual product/collection data this migration
already extracted, not placeholder Lorem Ipsum), commit, move to the next.
Don't build the entire theme in one uncommitted pass — the same "complete
one task, validate it, document it, commit it" discipline used in Phases
1–6.5 applies here.

## Preserve business functionality, improve where the data justifies it

Every WooCommerce feature in the App Replacement Matrix
(`docs/SHOPIFY_FOUNDATION.md`) needs a conscious decision — native Shopify
feature, an app, or "genuinely not needed" — not a silent drop. Where
Shopify's native capability is better than the WooCommerce plugin it
replaces (e.g. native abandoned-checkout email vs. a dedicated cart-recovery
plugin), take the native path; don't reintroduce plugin-equivalent
complexity to match WooCommerce's exact old mechanism when Shopify already
does the job.

## Accessibility & performance are requirements, not polish

- Match or improve on WCAG 2.1 AA — the current site has an accessibility
  scanner plugin installed (`wp_ea11y_*`, inactive per the current
  plugin list but present, suggesting accessibility was previously a
  concern); don't regress it.
- New theme sections should be built mobile-first, given this is a
  wholesale/reseller catalog likely to see meaningful mobile traffic
  (unverified — check analytics once GA continuity is confirmed per the
  deployment checklist, don't assume).
- No render-blocking third-party scripts in `theme.liquid` without a
  specific, justified reason — the WooCommerce site's plugin count (34
  active plugins) is a cautionary example of feature creep; don't recreate
  that on Shopify by defaulting every WooCommerce plugin to an app.

## Content parity checks before marking a template "done"

For each of the 5 pages with existing Rank Math SEO copy (Homepage, About,
Shop, Brands, Contact, Shipping Policy, Privacy Policy, Terms & Conditions,
Cookie Policy — see `docs/SEO_STRATEGY.md`), carry the title/description
into Shopify's native SEO fields as part of building that page, not as a
follow-up task that's easy to forget. Fix the known-wrong Cookie Policy
description (risk #8) at the same time, not separately.

## What "done" means for Phase 7

A section/template is ready to merge when: it renders correctly against
real migrated data (not fixtures), passes Theme Check
(`docs/SHOPIFY_CODING_STANDARDS.md`), meets the accessibility bar above,
and its content sourcing matches what `docs/SHOPIFY_FOUNDATION.md` says it
should source from (metaobject vs. plain collection fields, etc.) —
all four, not just "looks right in the browser."
