# /shopify/

Migration artifacts and future theme development, kept separate from
`migration/` (the WooCommerce-side extraction pipeline) and `reports/`
(SEO/URL analysis).

- **`foundation/`** — machine-readable specs (collections, metafields,
  metaobjects, navigation) produced in Phase 6.5. Planning artifacts, not
  consumed by any script yet — Phase 9 tooling will read these when
  collections/products are actually created in Shopify. See
  `docs/SHOPIFY_FOUNDATION.md` for the human-readable version and the
  reasoning behind each list.
- **`theme/`** — reserved for the Shopify theme itself (Phase 7). Empty
  until then; no Liquid/theme code is written before Phase 7 is approved.
- **`scripts/`** — reserved for future Admin/GraphQL API setup scripts
  (collection creation, metafield/metaobject definition, redirect import —
  Phase 9). Empty until then.

No customer PII, SQL dumps, or generated import CSVs belong in this
directory — those stay under `migration/` and `shopify-theme/assets/`,
both git-ignored. Everything under `/shopify/` is safe to commit.
