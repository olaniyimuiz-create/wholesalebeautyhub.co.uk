# shopify-theme/assets/

`csv_generator.py` writes `shopify_products_import.csv` and
`shopify_customers_import.csv` here, ready for Shopify Admin's CSV importer.

Those two files are git-ignored (customer PII). Theme assets added later
(images, CSS, JS) are not covered by that rule and should be committed
normally.
