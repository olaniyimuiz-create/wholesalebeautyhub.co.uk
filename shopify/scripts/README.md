# shopify/scripts/

Reserved for Phase 9 Admin/GraphQL API setup scripts — creating
collections, metafield/metaobject definitions, and importing redirects
directly via the API rather than CSV, driven by the specs in
`shopify/foundation/`. Intentionally empty; nothing here executes against
the live Shopify store until Phase 9.

Not to be confused with `migration/scripts/` (the WooCommerce-side
extraction pipeline: `sql_utils.py`, `database_parser.py`,
`csv_generator.py`, `seo_url_mapper.py`) — that pipeline is unrelated to
this directory and out of scope for changes here.
