# Architecture

## Why no database server

The Adminer export is a single ~1.6GB `.sql` file. Rather than standing up a
MySQL instance to load it, `migration/scripts/sql_utils.py` streams the file
directly. This works because Adminer writes one row tuple per line and
escapes embedded newlines as literal `\n`, so each `(...)` row is guaranteed
to be exactly one line — no full SQL grammar is needed, just a per-line
tokenizer for quoted values (`split_row_values`) and a table/column extractor
for `INSERT INTO ... (...) VALUES` headers (`iter_insert_rows`).

`iter_insert_rows` skips rows for tables it wasn't asked for with an O(1)
check (does the line end in `;`), so scanning past `wp_postmeta`'s 1.35M rows
looking for the ~1,500 that matter costs a single pass, not a full parse of
each irrelevant row.

`php_unserialize` is a minimal PHP `unserialize()` (arrays, strings, ints,
floats, bools, null) needed to read WooCommerce's serialized
`_product_attributes` meta value. It operates on UTF-8 bytes rather than
Python `str` indices because PHP's serialized string lengths are byte counts,
which would misalign on any non-ASCII attribute name/value otherwise.

## Data flow

```
dump.sql --[database_parser.py]--> products.json, customers.json --[csv_generator.py]--> *.csv
```

### `database_parser.py`

Reads these tables (see `TARGET_TABLES`), filtering `wp_postmeta` /
`wp_usermeta` down to a meta_key whitelist since those two tables dominate
the dump's size:

| Table | Used for |
|---|---|
| `wp_posts` | products, variations, attachments (filtered to those post_types) |
| `wp_postmeta` | price, stock, SKU, weight, images, serialized attributes, variation option values |
| `wp_terms` / `wp_term_taxonomy` / `wp_term_relationships` | categories (`product_cat`), tags (`product_tag`), brand (`pwb-brand`), product type (`product_type`) |
| `wp_wc_product_meta_lookup` | fallback SKU/price/stock/tax when postmeta is missing it |
| `wp_woocommerce_attribute_taxonomies` | human-readable labels for `pa_*` attributes (e.g. `pa_shade` → "Shade") |
| `wp_users` / `wp_usermeta` | registered customer profiles, billing/shipping address, role (`wp_capabilities`) |
| `wp_wc_customer_lookup` | denormalized customer view — covers both registered *and* guest checkouts |
| `wp_wc_order_addresses` | fills in address/phone for guest customers, who have no `wp_usermeta` row |

Customers are deduplicated by email, and accounts whose only WordPress role
is staff (`administrator`, `shop_manager`, `editor`) are excluded — they show
up in the dump because they placed test orders, not because they're
customers.

### `csv_generator.py`

Maps the JSON into Shopify's documented CSV import columns (verified against
`help.shopify.com` at implementation time, not from memory):

- **Products**: one row per variant under a shared `Handle`; the first row
  per handle carries the product-level fields (Title, Body, Vendor, Tags,
  first image). `_regular_price` vs `_price` becomes Compare-At vs Price so
  sale pricing survives the move. `manage_stock != 'yes'` maps to a blank
  `Variant Inventory Tracker`, which tells Shopify not to track inventory —
  matching WooCommerce's "always in stock" behaviour for unmanaged products,
  rather than importing a fake quantity.
- **Customers**: `Accepts Email Marketing` / `Accepts SMS Marketing` are
  hardcoded to `no` — WooCommerce doesn't reliably track marketing consent in
  the fields this pipeline reads, and the store is UK-based (GDPR), so
  defaulting to no consent is the safe choice rather than assuming opt-in.

## Known limitations

- Product `Type` is taken from the product's first WooCommerce category —
  there's no separate free-text "type" field in WooCommerce to map from.
- `Default Address Province Code` is passed through as WooCommerce's raw
  billing state/county (e.g. "England", "Wales") since GB addresses don't
  have Shopify-recognized province codes; harmless but not normalized.
- Grouped and external/affiliate product types aren't specifically handled
  yet (`wc_type` is captured but only `simple`/`variable` are branched on).
