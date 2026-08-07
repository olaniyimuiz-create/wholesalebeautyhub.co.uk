# Wholesale Beauty Hub — WooCommerce → Shopify Migration

Migrates [wholesalebeautyhub.co.uk](https://www.wholesalebeautyhub.co.uk) from
WooCommerce (WordPress) to Shopify: products, variants, images, categories,
brands, and customers, exported as Shopify-importable CSVs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pipeline works
internally, [docs/SEO_STRATEGY.md](docs/SEO_STRATEGY.md) for the URL
migration plan, [docs/SHOPIFY_ARCHITECTURE.md](docs/SHOPIFY_ARCHITECTURE.md)
for the target information architecture,
[docs/SHOPIFY_FOUNDATION.md](docs/SHOPIFY_FOUNDATION.md) for the concrete,
buildable spec that architecture produces,
[docs/SHOPIFY_BUILD_GUIDELINES.md](docs/SHOPIFY_BUILD_GUIDELINES.md) /
[docs/SHOPIFY_CODING_STANDARDS.md](docs/SHOPIFY_CODING_STANDARDS.md) /
[docs/SHOPIFY_DEPLOYMENT.md](docs/SHOPIFY_DEPLOYMENT.md) for how Phase 7+
should be built and shipped, [docs/DECISIONS.md](docs/DECISIONS.md) for why
things were built the way they were, and
[docs/MIGRATION_PROGRESS.md](docs/MIGRATION_PROGRESS.md) for task status and
the Phase 6.5 readiness report. Remaining phases (7–13) are tracked as
[GitHub Milestones and Issues](https://github.com/olaniyimuiz-create/wholesalebeautyhub.co.uk/milestones)
with explicit acceptance criteria and dependencies.

## Status

- **Data pipeline** (SQL dump → Shopify CSVs): built and validated against
  the live store's export — 611 products (497 variations) and 12,096
  customers, parsed in ~19s with zero malformed CSV rows.
- **SEO & URL mapping**: built and validated — 875-row redirect matrix plus
  duplicate/orphan/broken-link reports in `reports/`, cross-checked against
  the live site.
- **Shopify information architecture**: approved — brand architecture
  (ADR-006), category hierarchy cleanup, and the slug collision (ADR-007)
  are all resolved.
- **Shopify foundation**: concrete collection (156), metafield, metaobject,
  and navigation specs compiled in `docs/SHOPIFY_FOUNDATION.md` and
  `shopify/foundation/`. Build/coding/deployment standards written for
  Phase 7 onward. 7 new data-quality findings from this pass are gating
  for Phase 9, not Phase 7 — see `docs/RISK_REGISTER.md` risks #14–20.
- Theme development (`shopify/theme/`, reserved but empty), collection/
  product import, and Shopify API integration are not started yet.

## Prerequisites

- Python 3.10+
- An Adminer MySQL export of the WooCommerce database

## Usage

1. Export the WooCommerce database via Adminer and save it as
   `migration/sql/dump.sql`.
2. Run the pipeline:

   ```bash
   ./gemini-code-1786108559105.sh
   ```

   or run the two stages directly:

   ```bash
   python migration/scripts/database_parser.py   # dump.sql -> migration/data/*.json
   python migration/scripts/csv_generator.py      # *.json -> shopify-theme/assets/*.csv
   python migration/scripts/seo_url_mapper.py     # dump.sql + products.json -> reports/*.csv
   ```

3. Import `shopify-theme/assets/shopify_products_import.csv` and
   `shopify_customers_import.csv` via Shopify Admin → Products/Customers →
   Import. `reports/redirect_matrix.csv` is shaped for Shopify's URL
   Redirect bulk importer but isn't meant to be imported yet — read
   `docs/SEO_STRATEGY.md` first, there's an open slug collision to resolve.

## Project layout

```
migration/
  sql/        Adminer dump (git-ignored, contains PII)
  data/       Parsed intermediate JSON (git-ignored, contains PII)
  scripts/    The WooCommerce-side extraction pipeline
shopify-theme/
  assets/     Generated Shopify import CSVs (git-ignored, contains PII)
shopify/
  foundation/ Machine-readable collection/metafield/metaobject/nav specs
  theme/      Reserved for Phase 7 (empty)
  scripts/    Reserved for Phase 9 Admin API setup (empty)
reports/      SEO/URL analysis reports (safe to commit - no PII)
docs/         Architecture, foundation, build/coding/deployment standards,
              SEO strategy, decisions, progress, risk register
```

Folders are added as tasks need them rather than scaffolded up front.

## Data protection

`migration/sql/dump.sql`, `migration/data/*.json`, and the generated import
CSVs all contain real customer data (names, emails, phone numbers, physical
addresses) and are excluded via `.gitignore`. Never commit them.
