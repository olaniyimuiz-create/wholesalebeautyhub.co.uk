# Wholesale Beauty Hub — WooCommerce → Shopify Migration

Migrates [wholesalebeautyhub.co.uk](https://www.wholesalebeautyhub.co.uk) from
WooCommerce (WordPress) to Shopify: products, variants, images, categories,
brands, and customers, exported as Shopify-importable CSVs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pipeline works
internally and [docs/MIGRATION_PROGRESS.md](docs/MIGRATION_PROGRESS.md) for
task status.

## Status

Data pipeline (SQL dump → Shopify CSVs) is built and validated against the
live store's export: 611 products (497 variations) and 12,096 customers,
parsed in ~19s with zero malformed CSV rows. Theme, redirects, and Shopify
API integration are not started yet.

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
   ```

3. Import `shopify-theme/assets/shopify_products_import.csv` and
   `shopify_customers_import.csv` via Shopify Admin → Products/Customers →
   Import.

## Project layout

```
migration/
  sql/        Adminer dump (git-ignored, contains PII)
  data/       Parsed intermediate JSON (git-ignored, contains PII)
  scripts/    The pipeline itself
shopify-theme/
  assets/     Generated Shopify import CSVs (git-ignored, contains PII)
docs/         Architecture notes and migration progress log
```

Folders are added as tasks need them rather than scaffolded up front.

## Data protection

`migration/sql/dump.sql`, `migration/data/*.json`, and the generated import
CSVs all contain real customer data (names, emails, phone numbers, physical
addresses) and are excluded via `.gitignore`. Never commit them.
