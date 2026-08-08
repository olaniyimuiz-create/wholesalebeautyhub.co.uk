# Phase 9 — Pre-Import Environment Readiness

Status as of 2026-08-08: **BLOCKED**. This document exists because a real
test import cannot start yet, and says exactly why, in enough detail that
whoever provisions the store/credentials can act on it directly.

This is explicitly **not** Phase 10. Nothing in this document authorizes or
performs a real import, touches the live WooCommerce site, or creates any
Shopify data. It defines what "ready" means, builds the tooling to verify
it, and records that none of it is true yet. `docs/PHASE9_IMPORT_STRATEGY.md`
already covers the CSV-vs-API decision and the production approval
requirements — this document doesn't repeat that; it covers the
environment/credential prerequisites and the test-import-specific
mechanics that sit underneath that decision.

## A. Development/test store

**Status: Does not exist.** No `shopify.app.toml`, no store domain, no
reference to any `*.myshopify.com` address anywhere in this repository or
in any prior phase's output. A real import — even a test one — needs a
Shopify development store (free, via the Partner Dashboard) or a paid
store with a protected password, separate from the live production store.

Required before Step 9's approval gate can be checked off:
- A Partner Dashboard account (or existing store owner access) creates a
  development store.
- The store's `.myshopify.com` domain is recorded (in `.env`, never in
  Git — see § E).

## B. Production store

**Status: Does not exist**, and out of scope for this document entirely.
No production import happens until Phase 9's test import is reconciled
and separately approved (§ I, and `docs/PHASE9_IMPORT_STRATEGY.md` §
Production approval requirements). Do not conflate provisioning a test
store with provisioning production — they are different stores, different
credentials, and different approval gates.

## C. Admin API access

**Status: Does not exist.** Admin API access requires a custom app
created in the target store (Settings → Apps and sales channels → Develop
apps), which issues an Admin API access token scoped to specific
permissions. This is per-store — a token from one store does not work
against another. Until § A is done, this step cannot start.

## D. Required API scopes

Based on what this project's dry-run payload actually contains
(`docs/PHASE9_PRODUCT_IMPORT.md`, `migration/data/products.json`) — not a
generic "grant everything" list:

| Scope | Why |
|---|---|
| `read_products`, `write_products` | Create/update products and variants |
| `read_product_listings` | Verify publication status |
| `read_inventory`, `write_inventory` | Set stock levels per variant |
| `read_metaobjects`, `write_metaobjects` | `brand` metaobject (ADR-007/009) and its references |
| `read_files`, `write_files` | Upload product images (Phase 8 media) |

Not requested: any customer, order, discount, or Markets/B2B scope — none
of those are in scope for Phase 9 product import, and requesting broader
access than needed is itself a risk. `migration/scripts/phase9_preflight.py`
checks the installed app has exactly this set (§ Admin API pre-flight
below) and fails loudly if any are missing, rather than assuming.

## E. Credentials/secrets

**Design implemented, nothing populated with real values.**

- `.env.example` (tracked in Git) documents every variable a script in
  this project needs, as an empty placeholder — `SHOPIFY_ENVIRONMENT`,
  `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_API_ACCESS_TOKEN`,
  `SHOPIFY_API_VERSION`, `SHOPIFY_WEBHOOK_SECRET`.
- `.gitignore` excludes `.env` and `.env.*` (any environment-specific
  variant), while explicitly re-including `.env.example` with `!.env.example`
  so the template itself stays tracked.
- No script in this project prints, logs, or writes a credential value to
  any file it produces. `migration/scripts/phase9_preflight.py`'s own
  report (`reports/phase9_preflight_result.json`) was checked by hand
  after a real run (§ Admin API pre-flight) and contains no token or
  domain value, only check names and pass/fail status.
- `SHOPIFY_ENVIRONMENT` exists specifically so a script can refuse to run
  against production when a test run was intended — not implemented as
  an enforced check yet (nothing calls a mutation at all today), but the
  field is there so that logic has somewhere to read from when it's built.

Verified this session: `git status` and `git log --all --oneline` show no
`.env` file, credential-shaped string, or token has ever been committed to
this repository.

## F. Store URL/domain

**Status: Unknown — depends on § A.** Not assumed to be
`wholesalebeautyhub.myshopify.com` or any other specific value; whatever
store is actually provisioned determines this. Recorded in `.env` only,
never hardcoded into a script or doc.

## G. Shopify plan requirements

**Status: Not decided by anyone.** No plan tier (Basic, Grow, Advanced,
Plus) has been chosen. This matters concretely for this project because:
- Bulk operations / GraphQL rate limits differ by plan and affect how a
  519-product import should be throttled (`docs/PHASE9_IMPORT_STRATEGY.md`
  § Rate limits).
- Multi-currency/Markets features referenced in ADR-010 are plan-gated.

This document does not assume Plus or any other tier. Whoever owns the
Shopify account decides this; it is not inferable from the migration data.

## H. Markets/B2B requirements

**Status: Open — ADR-010, unchanged since Phase 6.5.** Not assumed
approved. The theme and metafield schema are built to be config-ready
without Markets/B2B (`docs/DECISIONS.md` ADR-010), so this does not block
a single-market, single-currency test or production import of the core
catalog. It does block anything Markets/B2B-specific, which is not
attempted anywhere in this project.

## I. Backup/rollback requirements

Covered in detail in `docs/PHASE9_IMPORT_STRATEGY.md` § Rollback approach
and `docs/SHOPIFY_DEPLOYMENT.md` § Rollback strategy — not duplicated
here. Summary for the test-import case specifically: a development store
can simply be deleted and re-created if a test import goes wrong, which is
a real advantage of testing against a dev store rather than production
directly. No backup mechanism needs to be built before a **test** import;
production rollback strategy is a separate, already-documented gate.

## Admin API pre-flight tool

`migration/scripts/phase9_preflight.py` performs a **read-only** check of
the five things that matter most before any write is attempted:
authentication + store identity, granted scopes (§ D) match what's
required, and read access to products, collections, and metaobject
definitions. It performs zero mutations — this is enforced by the script
only ever issuing GraphQL queries, never a mutation, and the script prints
this explicitly on every run.

It reads `SHOPIFY_STORE_DOMAIN`/`SHOPIFY_ADMIN_API_ACCESS_TOKEN` from the
environment or a local `.env` (via a minimal stdlib-only loader — no new
dependency added). If either is missing, it exits immediately with status
`NOT_CONFIGURED` and writes that fact to
`reports/phase9_preflight_result.json` — it does not attempt a request, and
it does not print a fabricated pass.

**Real run performed this session** (2026-08-08, no credentials present in
this environment):

```
NOT_CONFIGURED: SHOPIFY_STORE_DOMAIN and/or SHOPIFY_ADMIN_API_ACCESS_TOKEN
are not set (checked environment and .env). This is the expected, honest
result in this environment - no Shopify store or credentials have been
provisioned for this project yet.
Wrote reports/phase9_preflight_result.json
```
Exit code 2. This is the only pre-flight result this project has ever
produced — no PASS result has been fabricated or claimed. When real
credentials exist, running the same script performs the five checks above
against the real store and reports PASS/FAIL per check; it still performs
no writes.

Points from the original 15-item pre-flight scope not implemented as
separate checks, and why:
- **API version compatibility**: the script pins the version it calls
  (`SHOPIFY_API_VERSION`, defaulting to `2025-01` only if unset) rather
  than "latest" — see `.env.example`'s comment on why pinning matters —
  and a version mismatch surfaces as a request failure in any check, so it
  doesn't need its own separate check.
- **Create/update products, variants, media, collections**: deliberately
  **not** tested by this script, anywhere, under any condition — a
  pre-flight tool that performs writes is the exact "destructive API call"
  this phase is prohibited from executing before human approval. Write
  capability is exercised for real, for the first time, only during the
  approved test import itself (§ Human approval gate), which is a
  different, later, explicitly-approved step — not folded into pre-flight.
- **Rate-limit handling, retry behavior, idempotency, error logging,
  dry-run mode**: these are properties of the *import client*, not the
  read-only pre-flight check — covered in § Import safety design below,
  to be implemented when the import client itself is built (after this
  gate passes).

## Import safety design

For when a real import client is built (not built yet — this section is
the design it must follow, per this phase's "design, don't build the live
import" boundary):

- **Idempotent matching key**: `custom.legacy_woo_id` metafield, per
  `docs/PHASE9_IMPORT_STRATEGY.md`. Before creating a product, the client
  queries for an existing product with that metafield value; if found, it
  updates rather than creates. This makes re-running the same import batch
  safe by construction, not by convention.
- **Checkpointing**: after each product is successfully created/updated,
  its WooCommerce ID and the resulting Shopify GID are appended to a
  checkpoint file (e.g. `reports/phase9_import_checkpoint.jsonl`, one line
  per record, flushed immediately — not buffered in memory) so a crashed
  or interrupted run can resume from the last completed record instead of
  restarting or re-processing everything.
- **Logging**: every request and its structured `userErrors` response (§
  Import Strategy's "Per-record error handling" row) logged to a run-scoped
  log file, keyed by `woo_product_id`. No credential value is ever written
  to this log — the same rule as § E, applied to a future script, not just
  `phase9_preflight.py`.
- **Retry strategy**: transient failures (HTTP 429/5xx) retried with
  backoff, bounded (e.g. 3 attempts); a `userErrors` validation failure
  (e.g. a genuinely bad field value) is not retried automatically — it's
  logged and the batch continues with the next record, since retrying a
  deterministic validation error just wastes calls and hides a real data
  problem.
- **Partial-import recovery**: because matching is by `legacy_woo_id` (not
  by position/order), a batch that failed partway through can simply be
  re-run in full — already-created records are matched and updated
  (harmlessly, since the payload is the same), not duplicated.
- **Duplicate detection**: the same `legacy_woo_id` query used for
  idempotent matching doubles as duplicate detection — if a query for a
  given `legacy_woo_id` unexpectedly returns more than one product, the
  client must stop and flag it rather than guessing which one to update.
- **Rollback strategy**: delete-by-`legacy_woo_id`-query, per
  `docs/PHASE9_IMPORT_STRATEGY.md` § Rollback approach — not duplicated
  here.

## Test import product set

Selected from real `migration/data/products.json` records (not invented) —
`reports/phase9_test_import_set.csv`, 9 products covering: a baseline
simple product, a variable product with 9 real variants, a real sale price
+ bundle/metafield candidate, multi-image handling, the zero-image case,
vendor/category/tag mapping, one of the 24 price-integrity-flagged IDs
(risk #24), the known ambiguous-vendor placeholder case (risk #31, deliberately
included to prove the import surfaces it rather than silently guessing a
vendor), and the AVIF-featured-image + draft-status combination. Full
justification per product is in the CSV's `notes` column.

## Reconciliation report template

`reports/phase9_reconciliation_template.csv` — schema only, zero data
rows, because no test import has been executed. Long-format: one row per
`(product, field)` comparison —
`woo_product_id,shopify_product_gid,handle,field,expected_value,actual_value,match,notes`.
`woo_product_id=ALL` rows are for aggregate checks (total product count,
total variant count). Fields to be checked once a real test import runs:
product count, variant count, SKUs, titles, descriptions, prices,
compare-at prices, inventory, vendors, product types, tags, collections,
images, handles, metafields, and publication status — matching this
phase's required reconciliation scope exactly.

**No reconciliation has been performed.** This template will only be
populated after a real test import against a real store, comparing this
project's source data (`expected_value`) against what Shopify's Admin API
actually reports back (`actual_value`) — not two derived-from-the-same-source
values, which is what `reports/phase9_reconciliation.csv` (Phase 9 dry run)
already correctly documents itself as being, not to be confused with this
template. Do not claim any reconciliation percentage — including 100% —
until this template has real rows in it, generated from a real API
response.

## Human approval gate — test import

All eight required items, current state:

- [ ] Dev/test store provisioned (§ A)
- [ ] Admin API credentials available securely (§ C, § E)
- [ ] Admin API permissions verified (§ D — verifiable via
      `phase9_preflight.py` once credentials exist)
- [ ] Admin API import method approved (recommendation made —
      `docs/PHASE9_IMPORT_STRATEGY.md` — but not yet explicitly approved
      by the store owner)
- [ ] Shopify plan tier decided (§ G)
- [ ] Markets/B2B scope decided (§ H, ADR-010)
- [ ] Test product set approved (candidate set built —
      `reports/phase9_test_import_set.csv` — not yet approved by the
      store owner)
- [ ] Test import explicitly approved

**Zero of eight items are checked.** Per this phase's stop condition: if
any required item is missing, stop — do not continue automatically, and do
not attempt to bypass this gate. This document stops here.

## What this phase did not do

No Shopify store was created. No Admin API request beyond the read-only
pre-flight's honest `NOT_CONFIGURED` exit was attempted. No credential was
fabricated, hardcoded, logged, or committed. No product, customer, or order
was imported anywhere. No WooCommerce data was modified. No DNS or
production configuration was touched. Phase 10 was not started.
