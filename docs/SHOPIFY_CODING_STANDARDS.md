# Shopify Coding Standards

Applies to everything under `shopify/theme/` from Phase 7 onward. Mirrors
the same engineering discipline used in `migration/scripts/` (small,
readable, no speculative abstraction) rather than introducing a different
standard for a different language.

## Theme Check is mandatory, not advisory

Every theme change must pass [Theme Check](https://shopify.dev/docs/storefronts/themes/tools/theme-check)
with zero errors before merge. Warnings should have a reason if left
unaddressed (documented in the PR, not silently ignored). Run it locally
(`shopify theme check`) before pushing, not just in CI.

## File organization

Standard Online Store 2.0 layout:
```
shopify/theme/
  layout/       theme.liquid, checkout.liquid (if customizing)
  templates/    JSON templates (product.json, collection.json, etc.)
  sections/     one concern per section file
  snippets/     small, reusable, no section-level logic
  blocks/       reusable content blocks referenced from sections
  assets/       compiled CSS/JS, static images
  locales/      en.default.json at minimum
  config/       settings_schema.json, settings_data.json
```

Template variants match `docs/SHOPIFY_ARCHITECTURE.md`: `product.json` +
`product.combo-deal.json`; `collection.json` + `collection.brand.json` +
`collection.promo.json`. Don't add a template variant that isn't backed by
an architecture decision — if a new one seems needed, that's an ADR, not a
quiet addition.

## Naming conventions

- **Handles** (files, sections, blocks): lowercase, hyphenated —
  `product-card.liquid`, not `ProductCard.liquid` or `product_card.liquid`.
- **Section/block IDs in schema**: match the file's handle.
- **Metafield/metaobject references in Liquid**: always through the exact
  namespace.key defined in `shopify/foundation/metafields.json` /
  `metaobjects.json` — no ad hoc metafields invented at the template
  layer. If a template needs data that isn't in those files, add it there
  first (see `docs/SHOPIFY_BUILD_GUIDELINES.md` § Foundation-first).
- **Collection/product handles**: sourced from the migration pipeline
  (`migration/scripts/`) and `shopify/foundation/collections.json` —
  never hardcoded a second time in theme code. Reference
  `{{ product.type }}`, `{{ collection.handle }}`, etc. dynamically.

## Liquid style

- Prefer `{%- -%}` / `{{- -}}` whitespace control by default in
  loops/conditionals to keep rendered HTML clean — don't leave stray
  blank lines in production markup.
- No inline `<style>`/`<script>` blocks in sections beyond what's
  genuinely section-scoped (e.g. a CSS custom property block for
  merchant-configured colors); shared styles/scripts belong in
  `assets/`.
- Section schema `settings` should have sensible `default` values so a
  freshly-added section never renders empty/broken before a merchant
  configures it.
- No comments explaining *what* Liquid does — like the Python pipeline,
  write self-explanatory code (clear variable/section names) and reserve
  comments for non-obvious *why* (a workaround, a Shopify platform quirk,
  a specific reason a value is hardcoded).

## JavaScript

- Vanilla JS or minimal dependencies — this is a mid-sized catalog
  (519 products), not a scale that justifies a heavy framework bundle.
- No polyfills for browsers Shopify's own supported-browser baseline
  already covers.
- Progressive enhancement: core storefront functions (view product, add
  to cart, checkout) must work with JS disabled/failed to load. Filtering
  and other Search & Discovery UI can be JS-dependent since Shopify's
  native filter apps already assume that baseline.

## CSS

- CSS custom properties for anything theme-editor-configurable (colors,
  spacing scale) — not hardcoded values duplicated across sections.
- Mobile-first media queries, matching the accessibility/performance
  stance in `docs/SHOPIFY_BUILD_GUIDELINES.md`.

## Commit & PR conventions

Same format already used across this repository (see `git log`):
`type(scope): summary`, e.g. `feat(theme): add brand collection template`,
`fix(theme): correct combo-deal metafield reference`. Reference the ADR or
risk-register item a change addresses when there is one, the same way
Phase 5/6 commits reference ADR-00x.
