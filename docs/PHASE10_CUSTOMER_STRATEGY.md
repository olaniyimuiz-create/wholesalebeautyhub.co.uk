# Phase 10 — Customer Import Strategy

Covers: import method recommendation (Step 6), idempotency design
(Step 7), quarantine rules (Step 8), reconciliation framework (Step 13),
rollback design (Step 14). Evaluated independently for customers — not
assumed from the Phase 9 product decision, even though it reaches a
similar conclusion.

## 1. Import method — formal recommendation (NOT YET APPROVED)

| Criterion | Admin GraphQL API | Shopify CSV import |
|---|---|---|
| Field coverage | Full — `firstName`/`lastName`/`email`/`phone`/`tags`/`note`/`emailMarketingConsent`/`taxExempt`, plus `metafields` in the same call | Fixed column set per Shopify's documented customer CSV format; **no metafield column exists** — a custom `legacy_woo_customer_id` value cannot be set via CSV at all |
| Address support | `addresses: [CustomerAddressInput]`, multiple addresses per customer (billing + shipping both representable) | One default address per row (matches the pre-built CSV's current shape) |
| Idempotency | Query-before-write by email (or, once set, by the legacy-ID metafield) — proven pattern from Phase 9 | CSV import's own dedup behavior for customers is not something this project controls or has verified; re-running a CSV import is a real duplicate-creation risk with no available mitigation from this side |
| Error handling / retry | Per-record: check `userErrors`, classify, retry/quarantine — same proven pattern as Phase 9 | Background job with a job-level success/failure summary; no per-record programmatic control |
| Auditability | Full per-record log (matches `reports/phase9_bulk_import_log.jsonl`'s existing shape) | Shopify's import-history UI only — not queryable/auditable the same way |
| Rate limits | Standard GraphQL cost-based throttling — same mechanism already handled correctly in Phase 9 | Shopify-managed background job; opaque timing |
| Scale (12,096 records) | Proven at 596-product scale already this project; same batching/reconciliation pattern extends directly | Untested at this scale by this project; large CSV imports have historically had Shopify-side reliability issues with partial failures that are hard to diagnose after the fact |
| Reconciliation | Fresh query after each batch — same pattern as Phase 9 | Would still require a separate live query pass regardless, so CSV doesn't save this step |

**Recommendation: Admin GraphQL API**, via `customerCreate`, reusing
the exact operational pattern already proven for products in Phase 9
(batching, per-record logging, live reconciliation, `@idempotent`-style
discipline). This is not "the product method, reused without thinking" —
metafield-based idempotency is *decisive* here specifically because the
CSV path cannot represent it at all, which is a customer-specific
finding, not an inherited one. The pre-built `shopify_customers_import.csv`
remains useful as a **backup/manual-review artifact**, not the
primary import path.

**Required approval statement**:
> "I approve the Admin API/GraphQL customer import method for the
> Shopify development store wholesale-beautyhub.myshopify.com."

## 2. Required API scopes — a real, current blocker

Checked live: the app installation currently has:
`read_files, read_inventory, read_metaobjects, read_product_listings,
read_products, write_files, write_inventory, write_metaobjects,
write_products`.

**Neither `read_customers` nor `write_customers` is granted.** A
read-only customer query attempted during this audit
(`{ customers(first: 1) { ... } }`) returned `ACCESS_DENIED`. This is a
**technical blocker**, not a business decision — the app's scope
configuration must be expanded in Shopify Admin (mirroring how
`read_products`/`write_products` were originally provisioned for
Phase 9, tracked as issue #40) before any read-only verification, let
alone a write, becomes possible.

## 3. Idempotency design

**Match key hierarchy** (investigated, not assumed):

1. **Primary: email.** Shopify customers are fundamentally email-identified
   (it's the field Shopify itself treats as effectively unique in
   practice), and it's already this pipeline's own dedup key. A
   pre-write query — `customers(first: 1, query: "email:...")` or a
   full-store scan (same defensiveness precedent as Phase 9, which found
   Shopify's search-query syntax unreliable for products and switched to
   a full scan) — determines CREATE vs. UPDATE.
2. **Secondary, once established: `custom.legacy_woo_customer_id`
   metafield.** Set on every created customer (mirrors
   `custom.legacy_woo_id` on products exactly). After the first
   successful run, this becomes the authoritative match key for all
   subsequent runs — more robust than email alone, since it survives an
   email address changing later without losing the WooCommerce identity
   link.
3. **Phone is NOT used as a match key.** Shopify requires phone
   uniqueness but 63% of records have no phone at all, and phone re-use
   across different real people (e.g. shared household numbers) is a
   real risk email doesn't have to the same degree.

**Outcome behaviour** (deterministic per customer, no ambiguous cases):

| Case | Outcome |
|---|---|
| No existing Shopify customer matches (by legacy-ID metafield, then by email) | `customerCreate`, set `custom.legacy_woo_customer_id` |
| Existing customer found | `customerUpdate` — top-level fields only (name, phone, tags); **never** overwrite an address or consent state that may have been changed by the customer themselves post-migration, matching the same "don't clobber real activity" caution Phase 9 applied to already-created products |
| Existing customer found, but with a *different* email currently on file than the source record (should be rare — only possible after the legacy-ID metafield exists and a customer later changed their Shopify email) | **Quarantine, not overwrite** — an automated email change is exactly the kind of silent identity risk this project has consistently refused to guess |
| Duplicate/conflicting source rows (`duplicate_email_conflicting_identity`, `missing_email`) | Never reach the write step — quarantined upstream by the dry run |

## 4. Quarantine rules (implemented in `migration/scripts/phase10_customer_dry_run.py`)

| Rule | Disposition | Reason |
|---|---|---|
| Missing email | QUARANTINE | Can't create or match without one |
| Invalid email format | QUARANTINE | Same reason — 0 real occurrences found in this dataset, rule kept for defense-in-depth |
| Same email, conflicting name across two source rows | QUARANTINE | Genuine ambiguity — importing either identity without review risks attributing the wrong name/history |
| Staff/admin WordPress role | EXCLUDE | Permanently ineligible by rule, not a data problem — 3 found |
| Same email, same identity, repeat row (e.g. repeat guest checkout) | SKIP | Not an error — the first occurrence already covers it |
| Missing name, malformed-looking UK postcode | **Not quarantined** — informational note only | Shopify doesn't require either; blocking on them would be over-strict compared to the precedent already set for products (missing SKU/category were never blocking either) |

Every quarantined record carries: WooCommerce ID, email, reason,
classification, and a recommended human action —
`reports/phase10_customer_quarantine.csv`.

## 5. Reconciliation framework

Same shape as `phase9_test_reconcile.py`/`phase9_bulk_reconcile.py`:
WooCommerce source → transformation (this dry run) → Shopify customer
(post-write) → **fresh, independent** Shopify query (never the
importer's own report). Field-by-field, each row classified `MATCH` /
`MISMATCH` / `UNRESOLVED` / `NOT_APPLICABLE` — never rounded to "passed"
if any mismatch exists. Template with the exact field list (legacy ID
metafield, email, name, phone, billing/shipping address, tags, note,
metafields, `emailMarketingConsent.marketingState`) is pre-built:
`reports/phase10_customer_reconciliation_template.csv`. Populated for
real only after an approved test import — currently all rows read
`NOT_APPLICABLE` since no write has occurred.

## 6. Rollback design

**Shopify customer deletion is destructive and is explicitly not the
default rollback mechanism**, per instruction and per this project's
own established Phase 9 precedent (never mass-delete as a recovery
strategy).

What this pipeline maintains instead, mirroring the proven product
pattern:

- **Import manifest** (`reports/phase10_customer_manifest.csv`) — every
  source row's intended disposition, generated fresh before any write.
- **Created-customer ledger** — WooCommerce `customer_id`, resulting
  Shopify customer GID, timestamp, batch ID, action (CREATED/UPDATED),
  written incrementally (same append-only `.jsonl` pattern as
  `reports/phase9_bulk_import_checkpoint.jsonl`), enabling resume without
  re-processing already-handled records.
- **Execution log** — every mutation attempt, its `userErrors` (if any),
  and retry count, matching the append-only pattern of
  `reports/phase9_test_import_log.jsonl`.
- **Reconciliation state** — the field-level comparison output after
  every batch.

**What this allows**: controlled, targeted remediation — e.g. a specific
mis-imported customer can be identified precisely (its Shopify GID is
known from the ledger) and corrected via a deliberate `customerUpdate`,
or, if genuinely necessary, a deliberate single `customerDelete` decided
case-by-case by a human, never an automated bulk rollback.

**What this does NOT allow**: undoing customer-initiated activity that
happens *after* import (e.g. a customer updating their own address, or
placing an order using their migrated account) — the ledger records
what *this pipeline* wrote, not everything that happens to the customer
record afterward. This limitation is inherent to any external
system integration, not specific to this design.
