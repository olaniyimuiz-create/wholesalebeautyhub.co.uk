# Shopify Deployment Guide

How the migration actually gets executed and shipped, once Phase 7 (theme)
and Phase 9 (import) are underway. This is process documentation, not
executed against the live store by this phase — Phase 6.5 is architecture
and tooling setup only.

## Import sequence

Order matters — each step depends on the ones before it existing in the
target Shopify store. Skipping ahead (e.g. importing products before
metafield definitions exist) means re-running the step once the dependency
is in place, or living with incomplete data.

1. **Metafield & metaobject definitions** (`shopify/foundation/metafields.json`,
   `metaobjects.json`) — created via Admin API/GraphQL or manually in
   Settings → Custom data. Must exist before any product references them.
2. **Shipping profiles & tax settings** — native Shopify configuration
   matching WooCommerce's weight-based shipping and UK VAT setup. Blocks
   product import only insofar as pricing/tax display depends on it; not a
   hard technical dependency, but get it right before go-live traffic.
3. **Collections** (`shopify/foundation/collections.json`) — category and
   brand Smart Collections, plus the 3 manual promo collections. Created
   before products so products auto-populate into rule-based collections
   on import rather than needing a second pass.
4. **Data cleanup** (blocking, do before step 5): the 2 brand
   reclassifications (ADR-007), the `unnamed`-slug brand fix, the
   Topicrem/Tropicrem/Topicream consolidation, the Product Type source
   fix in `csv_generator.py` (risk #9), and the category cleanup mapping
   application — all listed in `docs/RISK_REGISTER.md`. Fix in the source
   pipeline, then regenerate `products.json`/the product CSV, don't patch
   the CSV by hand.
5. **Products** — via `shopify-theme/assets/shopify_products_import.csv`
   (regenerated after step 4) or the Admin API, per whatever Phase 7/9
   decides for the API-vs-CSV question (task 7 in `docs/MIGRATION_PROGRESS.md`,
   not yet decided).
6. **Customers** — `shopify_customers_import.csv`, independent of products,
   can run in parallel with step 5.
7. **Navigation** (`shopify/foundation/navigation.json`) — after
   collections/pages exist, since menu items link to them.
8. **Redirects** (`reports/redirect_matrix.csv`) — after products,
   collections, and pages all exist at their final handles, so redirect
   targets actually resolve. Import last among the content steps.
9. **Order history** — per whatever Phase 11 decides (Matrixify /
   Transporter / Admin API / accept the gap) — independent of the above,
   can happen before or after go-live depending on the chosen method.
10. **QA pass** (Phase 12) — before DNS cutover, not after.
11. **DNS cutover** — last. See rollback strategy below for what happens
    if something's wrong after this step.

## Rollback strategy

**Before DNS cutover (steps 1–10)**: fully reversible. Nothing in Shopify
is customer-facing yet — WooCommerce remains the live site. If a step
produces bad data (wrong collection rules, broken redirects, malformed
products), fix the source and re-import; Shopify's bulk operations don't
require "undoing" anything since the WooCommerce site was never affected.

**After DNS cutover**: this is the point of no easy return, so minimize
time spent in a state where rollback is needed:

- Keep WooCommerce fully intact and reachable at its original hosting for
  a minimum defined window post-cutover (recommend 30 days) — don't
  decommission the WordPress hosting/database immediately.
- Keep DNS TTL low (e.g. 300s) in the 24–48h around cutover specifically
  so a revert (pointing DNS back at the WooCommerce host) propagates fast
  if something is wrong.
- Orders placed on Shopify during a rollback window are the hard part —
  there's no automatic way to replay a Shopify order back into
  WooCommerce. Define the rollback decision point (e.g. "roll back only if
  a P0 issue is found within 2 hours of cutover, before real orders
  accumulate") before cutover, not during an incident.
- The redirect matrix (`reports/redirect_matrix.csv`) is one-directional
  (WooCommerce → Shopify). A DNS rollback needs the *reverse* — WordPress's
  own redirect handling reverted to serve the original URLs again, which
  means not deleting/renaming WooCommerce content during the transition
  window even though it's no longer the live site.

## Deployment checklist

Pre-launch, in order:

- [ ] All items in `docs/RISK_REGISTER.md` are Resolved or explicitly
      accepted (not silently ignored)
- [ ] `docs/SHOPIFY_FOUNDATION.md` collection/metafield/metaobject/
      navigation specs match what's actually configured in the Shopify
      Admin (spot-check a sample, don't assume the plan == the build)
- [ ] Product CSV regenerated after the Phase 9 data-cleanup fixes (not
      the one currently in `shopify-theme/assets/` from Phase 3/4 testing)
- [ ] Redirect matrix re-run (`seo_url_mapper.py`) against final handles
      and re-validated for 0 duplicates before import
- [ ] Theme passes Theme Check with no errors (`docs/SHOPIFY_CODING_STANDARDS.md`)
- [ ] Shipping rates and UK VAT/tax settings configured and verified with
      a real test order
- [ ] Payment provider (Shopify Payments) live and tested
- [ ] Google Search Console: new sitemap submitted, property ownership
      re-verified if the domain's hosting changed
- [ ] Analytics/GTM continuity confirmed (no gap in tracking across
      cutover)
- [ ] Customer-facing legal pages (Privacy, Terms, Shipping, Refunds,
      Cookie Policy) reviewed for accuracy, including fixing the known
      wrong Cookie Policy meta description (risk #8)
- [ ] Rollback decision point and owner agreed *before* cutover, not
      during an incident
- [ ] DNS TTL lowered ahead of the cutover window
- [ ] WooCommerce hosting/database explicitly NOT decommissioned yet —
      scheduled for removal only after the post-cutover window closes

## Shopify CLI & environment workflow (Phase 7+)

- Theme development uses the Shopify CLI (`shopify theme dev` for local
  preview against a development theme on the live store, `shopify theme
  push`/`pull` for syncing).
- Use a **named development theme** during Phase 7, not the live/published
  theme — `shopify theme dev` creates one automatically. Never develop
  directly against the published theme.
- Promote to a duplicate "staging" unpublished theme for stakeholder
  review before publishing live.
- Theme version history in Shopify Admin is not a substitute for git — the
  theme's source lives in `shopify/theme/` in this repository and that's
  the source of truth; Shopify's own version list is a convenience/rollback
  aid, not where changes originate.
