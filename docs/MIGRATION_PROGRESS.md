# Migration Progress

## Task tracker

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | SQL dump → products/customers CSV pipeline | Done | 611 products / 497 variations / 12,096 customers, validated end-to-end |
| 2 | Theme migration (Shopify Liquid theme) | Not started | |
| 3 | Collections / navigation menus | Not started | Blocked on resolving the brand/category slug collision — see risk #5 |
| 4 | Blog + static pages | Not started | 9 pages have existing Rank Math SEO copy worth preserving — see `docs/SEO_STRATEGY.md` |
| 5 | SEO: URL redirect map (WooCommerce → Shopify slugs) | Done | 875-row redirect matrix + orphan/duplicate/broken-link reports in `reports/`; see `docs/SEO_STRATEGY.md` |
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

### 2026-08-07 — Task 5: SEO & URL mapping

- Confirmed WooCommerce's actual permalink structure from `wp_options`
  (`/shop/` product base, `/product-category/`, `/product-tag/`) and
  spot-checked 4+ live pages to validate it, catching one case where the
  database's implied URL (a nested page path) didn't match what's actually
  served (ADR-005).
- Built `migration/scripts/seo_url_mapper.py` (reuses `sql_utils.py` and
  the existing `products.json`) to inventory products, categories, brands,
  tags, pages, and blog posts, and generate:
  `reports/redirect_matrix.csv` (875 rows), `reports/duplicate_urls.csv`,
  `reports/orphan_urls.csv`, `reports/broken_links.csv`.
- Parsed `wp_posts` `nav_menu_item` + `_menu_item_*` postmeta to check
  real navigability instead of only in-content links — cut the false
  "orphan" count from 872/875 to a defensible 80/875 by correctly
  excluding products/tags (normally not in a nav menu), WooCommerce
  utility pages (cart/account/checkout — reachable via header UI, not
  menus), and pages confirmed live in the theme's hardcoded footer.
- Found and documented: one real category/brand slug collision, the
  tag→collection mapping gap, the blog not being linked from the header
  menu, and a pre-existing wrong meta description on the live Cookie
  Policy page. Full detail in `docs/SEO_STRATEGY.md`.
- Wrote `docs/SEO_STRATEGY.md` and `docs/DECISIONS.md` (ADR-001 through
  ADR-005); moved the risk register out to `docs/RISK_REGISTER.md` and
  extended it with the findings above.

## Risk register

Moved to [`docs/RISK_REGISTER.md`](RISK_REGISTER.md).
