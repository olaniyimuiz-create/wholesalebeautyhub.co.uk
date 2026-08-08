# Phase 9 Import Strategy: CSV vs. Admin API

Decision required by task 7 in `docs/MIGRATION_PROGRESS.md`, open since
Phase 3. This document makes a recommendation; it does not execute a real
import either way — no Shopify store or API credentials have existed at
any point in this project (verified: no `shopify.app.toml`, no API
token, no store domain referenced anywhere in the repository or prior
session history). That absence is itself load-bearing for this decision,
not a detail to gloss over.

## Evaluation

| Criterion | CSV | Admin API (GraphQL) |
|---|---|---|
| Catalog scale (519 products, ~497 variants, 156 collections) | Comfortably within CSV's practical range | Also fine; GraphQL bulk operations handle far larger catalogs |
| Metafield support (`custom.brand`, `custom.included_items`, `custom.legacy_woo_id`) | Supported via specific CSV column syntax, but metaobject *references* (`custom.brand` → the `brand` metaobject) are not reliably settable through the standard product CSV — this is the single biggest practical gap | Native — a metafield of type `metaobject_reference` is just another field in the mutation payload |
| Collection assignment | Product CSV has no native "collections" column; manual/promo collections (ADR-007) need separate handling regardless of method | Native — `collectionAdd`/`productCreate` can set membership directly |
| Idempotent re-runs | Achievable: Shopify matches CSV rows to existing products **by Handle**, and this pipeline's handles are already deterministic (WooCommerce `post_name`, unchanged since Phase 3) — re-importing the same CSV updates rather than duplicates | Requires building the upsert logic explicitly (query by `custom.legacy_woo_id` metafield before create), but then gives full control over *what* counts as a match |
| Per-record error handling | Shopify reports import errors in the UI/via email after the fact — no structured per-row error object to log or branch on programmatically | Every mutation returns structured `userErrors` immediately, can be logged/retried per record |
| Retry behavior | Whole-file re-upload; no partial-record retry | Can retry individual failed records without re-touching successful ones |
| Rate limits | Not applicable (bulk file upload, not per-record calls) | Real constraint — needs bulk operations or careful throttling for 519+ product creates, addressable but real engineering work |
| Rollback | Delete or revert specific products manually / re-import a corrected CSV | Can delete-by-metafield-query programmatically, more precise |
| Preserving WooCommerce ID | Via a metafield column in the CSV — works, but confirming it round-trips correctly through Shopify's CSV importer needs a real test import to verify (not confirmed in this repo) | Set directly and reliably in the mutation payload |
| Operational complexity | Low — this pipeline already produces `shopify-theme/assets/shopify_products_import.csv` (Phase 3/4); import is "upload a file" | Higher — needs a custom app, API credentials, and a real script (none of which exist yet) |
| Auditability | Shopify's own import log; coarse-grained | Every request/response can be logged by this project's own tooling — finer-grained, matches this document's "controlled, repeatable, auditable" objective directly |

## Recommendation: Admin API (GraphQL)

For the specific requirements this phase was given — deterministic
upsert by a stable external ID, metaobject-referencing metafields,
explicit manual-collection membership, structured per-record error
handling and retry, and a real audit trail — the Admin API is the
better-fitting mechanism, not CSV. CSV remains a viable fallback for the
product/variant/price/basic-field data alone (Phase 3/4 already produce
a working CSV), but it cannot cleanly satisfy the metaobject-reference
and collection-assignment requirements without a secondary process
layered on top — at which point most of CSV's simplicity advantage is
gone anyway.

**This recommendation is conditional, not final**, on two things neither
this document nor any prior phase can resolve:

1. **A Shopify store must exist** with Admin API access configured (a
   custom app + access token, or equivalent). Nothing in this
   repository's history shows one being created. This is a real,
   external prerequisite — not a technical detail this pipeline can work
   around.
2. **Shopify plan tier** (open since ADR-010) doesn't block the CSV-vs-
   API choice itself (both are available on every plan), but affects
   related decisions the API implementation would need to know before
   being built for real — e.g. whether B2B-specific mutations are ever
   in scope.

Per this phase's stop conditions: **CSV vs. Admin API is not left
undecided** (a genuine recommendation is made above, satisfying the
requirement not to leave this ambiguous) — but *building the real API
integration and executing a real import* stops here pending store/
credential provisioning, which is a decision and an action for whoever
owns the Shopify account, not something to fabricate or simulate as
having happened.

## Limitations of this recommendation

- Not validated against a real store — the comparison above is derived
  from Shopify's documented capabilities (checked against current
  `shopify.dev` documentation during this phase, not from memory) and
  this project's own data shape, not from a live test.
- If, in practice, the store owner already has an established CSV-based
  workflow (e.g. via an existing app like Matrixify) that solves the
  metaobject/collection gaps CSV alone can't, that would be a legitimate
  reason to override this recommendation — flag it rather than assume.

## Rollback approach (once a real import exists)

Not yet applicable — nothing has been created in any Shopify store. Once
Steps 16/18 (test/production import) actually run, rollback means:
identify affected records by `custom.legacy_woo_id` (API) or by Handle
(CSV), and either delete them via the same mechanism that created them
or re-import a corrected payload. Full detail belongs in
`docs/SHOPIFY_DEPLOYMENT.md` § Rollback strategy once there's a real
import to roll back — that section already exists and doesn't need
new content until then.

## Dry-run approach

Fully achievable without a Shopify store or credentials — see
`docs/PHASE9_PRODUCT_IMPORT.md` and `migration/scripts/phase9_dry_run.py`.
The dry run parses, transforms, validates, and produces the exact payload
shape either method would need, without contacting Shopify at all.

## Production approval requirements

Before Steps 16 (test import) or 18 (production import) can happen:

1. Shopify store exists (test/development store for Step 16; the real
   store for Step 18).
2. Admin API credentials provisioned, scoped appropriately (or CSV
   access confirmed, if the recommendation above is overridden).
3. ADR-010 (Markets/B2B/plan tier) resolved to whatever extent it
   affects the specific fields being imported.
4. This phase's dry-run reports reviewed and any quarantined/flagged
   products resolved (`reports/phase9_product_data_quality.csv`).
5. Explicit, separate approval for test import, and again for production
   import — these are two different approval gates, not one.

**None of the above exist yet.** This document stops at the
recommendation; it does not proceed to build or run a live import
client.
