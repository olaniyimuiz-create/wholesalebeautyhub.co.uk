# Migration Progress

## Task tracker

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | SQL dump → products/customers CSV pipeline | Done | 611 products / 497 variations / 12,096 customers, validated end-to-end |
| 2 | Theme migration (Shopify Liquid theme) | Not started | |
| 3 | Collections / navigation menus | Not started | |
| 4 | Blog + static pages | Not started | |
| 5 | SEO: URL redirect map (WooCommerce → Shopify slugs) | Not started | Needed to preserve search ranking |
| 6 | Metafields / metaobjects for attributes not covered by variants | Not started | |
| 7 | Shopify Admin/GraphQL API integration (automated import vs. CSV) | Not started | CSV import is the current path |
| 8 | Order history migration | Not started | Shopify's CSV import doesn't support historical orders; needs an app or Admin API approach |
| 9 | Cutover plan (DNS, final sync, WooCommerce freeze) | Not started | |

## Change log

### 2026-08-07 — Task 1: data pipeline

- Inspected the real Adminer dump (`wp_posts`, `wp_postmeta`,
  `wp_wc_product_meta_lookup`, `wp_wc_customer_lookup`,
  `wp_wc_order_addresses`, term tables) to build the parser against actual
  schema and data rather than assumptions.
- Built `migration/scripts/sql_utils.py`, `database_parser.py`,
  `csv_generator.py`.
- Ran end-to-end against the live export: 1.6M rows scanned in ~19s, 611
  products / 497 variations / 12,096 customers, 0 malformed CSV rows
  (verified by re-parsing both outputs with Python's `csv` module).
- Added `.gitignore` for the dump, parsed JSON, and generated CSVs — all
  contain real customer PII.

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Customer/order PII committed to a public repo | High | `.gitignore` covers `dump.sql`, `migration/data/*.json`, and the generated CSVs; verify `git status` before every commit |
| Shopify CSV import silently drops/misreads a field Shopify's format changed since implementation | Medium | Re-verify column headers against `help.shopify.com` before each real import, not from memory |
| SEO loss from URL structure change (WooCommerce slugs vs Shopify) | High | Not yet addressed — Task 5 |
| Order history isn't importable via product/customer CSV | Medium | Needs a dedicated decision in Task 8 (migration app vs. Admin API vs. accept the gap) |
