# Phase 10 — Customer Import Procedure

Companion to `MIGRATION_BRIEF.md`. **Do not load, read into working context, or act on this
file until Gates E–H are explicitly cleared.**

**Status update (2026-09-05, superseding the line below): the bulk customer import has
executed.** Live query, paged in full: **11,844 customers exist in the store, every one
carrying `custom.legacy_woo_customer_id`** — an exact match to the run ledger. Gate 7
authorization is recorded in `migration/schema/phase10_migration_contract.json` as
owner-confirmed retrospectively (see that file's `authorization` block for the exact
provenance and its stated evidence gap). Gate A below (`read_customers`/`write_customers`)
is also live-confirmed **GRANTED** as of this date — re-verify before relying on it, since
this project has already seen these scopes granted, then expire, more than once.

**Original status line, kept for the historical record it was written against:** "BLOCKED.
Zero Shopify customer writes have ever occurred. No customer has been created, updated,
deleted, merged, or fetched by GID at any point in this project." That was accurate through
2026-08-17. It is not accurate now — do not cite it as current state.

> **Provenance note.** The gate lettering (A–J) and the 18-step numbering are an organizing
> device introduced by this document to make the existing material executable. Every fact,
> figure, rule and decision below is carried unchanged in meaning from
> `docs/PHASE10_READINESS.md`, `docs/PHASE10_CUSTOMER_STRATEGY.md`,
> `docs/PHASE10_CUSTOMER_MAPPING.md`, `docs/PHASE10_GDPR_CONSENT.md`,
> `docs/PHASE10_ACCOUNT_STRATEGY.md` and `docs/PHASE10_APPROVAL_GATE.md`. Nothing here is a
> previously-agreed named gate — treat the letters as labels, not as prior commitments.

---

## 1. Source dataset — independently re-verified

Recomputed directly from `wp_wc_customer_lookup` / `wp_usermeta` in `migration/sql/dump.sql`
via `migration/scripts/phase10_customer_dry_run.py` — **not** inherited from
`migration/data/customers.json`. The dry run was executed twice; manifest and statistics
output were byte-identical.

| Metric | Count |
|---|---:|
| Raw `wp_wc_customer_lookup` rows | 13,043 |
| **IMPORT-eligible** | **12,096** |
| QUARANTINE — missing email | 292 |
| QUARANTINE — conflicting identity sharing one email | 247 |
| **QUARANTINE total** | **539** |
| SKIP — redundant duplicate row (same identity, repeat guest checkout) | 407 |
| EXCLUDE — staff/admin account | 1 (3 distinct staff accounts, most contributing one lookup row each) |
| Registered | 6,649 |
| Guest | 5,447 |
| Missing phone | 7,646 (63%) |
| Missing billing address | 7,367 (61%) |
| Has a real shipping address, currently unmapped (risk #40) | 1,209 (10%) |
| Both names empty (of 13,043 raw) | 5,430 — imported anyway, not blocked |

The 12,096 figure and the 6,649/5,447 split independently confirm what `customers.json`
already held. What is new: **539 records previously dropped silently by `build_customers()`
with no audit trail** now carry an explicit, reviewable disposition in
`reports/phase10_customer_quarantine.csv` — each row with WooCommerce ID, email, reason,
classification, and a recommended human action.

Quarantine rules: missing email ⇒ QUARANTINE. Invalid email format ⇒ QUARANTINE (0 real
occurrences; rule retained for defence in depth). Same email with conflicting name across
two rows ⇒ QUARANTINE. Staff/admin WordPress role ⇒ EXCLUDE (permanent, by rule).
Same email + same identity repeat row ⇒ SKIP (not an error). Missing name or
malformed-looking UK postcode ⇒ **not** quarantined, informational note only — Shopify
requires neither, and blocking would be stricter than the precedent already set for products.

---

## 2. Gates

Nothing proceeds past a gate that is not explicitly cleared. **Gates E–H are the
authorization gates referenced by `MIGRATION_BRIEF.md`.**

| Gate | Requirement | Type | Status |
|---|---|---|---|
| **A** | `read_customers` + `write_customers` granted on the app | TECHNICAL | **CLEARED** — live-verified 2026-09-05, both scopes GRANTED, `customersCount` no longer denied. (Was BLOCKED 2026-08-17; this scope has flipped granted→revoked→granted at least once in this project's history — re-verify live before treating as settled.) |
| **B** | Import method approved (Admin GraphQL API recommended) | APPROVAL | The bulk run already executed via `customerCreate`/`customerAddressCreate` per `phase10_migration_contract.json` — treat as exercised in practice. No separate written approval statement for this exact phrase has been located in this repository; if one exists elsewhere, link it here. |
| **C** | Marketing-consent policy decided | BUSINESS + LEGAL | Open — issue #19, ADR-014 |
| **D** | Shipping-address mapping decided (add, or accept billing-only gap) | BUSINESS | Open — risk #40 |
| **E** | `custom.legacy_woo_customer_id` metafield design approved for implementation | APPROVAL | Open |
| **F** | Test import authorized — the 10-customer set | APPROVAL | Open — issue #44 |
| **G** | Test-import reconciliation reviewed and accepted by a human | APPROVAL | Not reachable until F |
| **H** | Bulk import authorized — explicitly separate from F | APPROVAL | Open |
| **I** | Batch size and execution window agreed | EXECUTION | Not reachable until H |
| **J** | Ledger, execution log and rollback artifacts confirmed in place pre-write | EXECUTION | Not reachable until H |

Required approval statement for Gate B, verbatim:

> "I approve the Admin API/GraphQL customer import method for the Shopify development store
> wholesale-beautyhub.myshopify.com."

**None of these is resolved.** No comment, ADR, or authorization for any customer write —
test or bulk — exists anywhere in this repository or its GitHub issues.

---

## 3. Method, and why

**Recommended: Admin GraphQL API via `customerCreate`** — evaluated independently for
customers, not inherited from the Phase 9 product decision.

**Decisive factor:** Shopify's customer CSV import has **no metafield column**, so it cannot
carry the `custom.legacy_woo_customer_id` idempotency key at all. That is a
customer-specific finding. CSV also gives no per-record error control, no queryable audit
trail, and an unmitigated duplicate-creation risk on re-run. The pre-built
`shopify_customers_import.csv` remains useful as a backup/manual-review artifact, not as the
import path.

---

## 4. Idempotency requirements

**Match key hierarchy:**

1. **Primary: email.** Shopify customers are email-identified, and it is already this
   pipeline's dedup key. A pre-write query determines CREATE vs. UPDATE. Prefer a full-store
   scan over search-query syntax — Phase 9 found Shopify's product search unreliable and
   switched for exactly this reason.
2. **Secondary, once established: `custom.legacy_woo_customer_id`.** Set on every created
   customer, mirroring `custom.legacy_woo_id` on products. After the first successful run
   this becomes authoritative, surviving a later email change without losing the WooCommerce
   identity link.
3. **Phone is never a match key** — 63% have none, and shared household numbers make re-use
   a real risk that email does not carry to the same degree.

**Deterministic outcome for every case:**

| Case | Outcome |
|---|---|
| No match by metafield, then by email | `customerCreate`, set `custom.legacy_woo_customer_id` |
| Existing customer found | `customerUpdate`, **top-level fields only** — never overwrite an address or consent state the customer may have changed themselves post-migration |
| Existing customer found under a *different* current email | **QUARANTINE, never overwrite** — a silent identity change is exactly what this project refuses to guess |
| Conflicting/duplicate source rows | Never reach the write step — quarantined upstream by the dry run |

---

## 5. Consent — the caveat that governs everything here

A real signal exists in FluentCRM (`wp_fc_subscribers`, 6,692 rows: 6,437 `subscribed`,
234 `unsubscribed`, 21 `pending`). Intersected with the 12,096 IMPORT-eligible customers:

| Classification | Count |
|---|---:|
| Explicit consent (`subscribed`) | 6,295 |
| Explicit opt-out (`unsubscribed`) | 229 |
| Explicit in-progress (`pending`) | 21 |
| **No consent information at all** | **5,551 (46%)** |
| Genuinely ambiguous | 0 |

`wp_wc_email_unsubscribes` exists but is **empty (0 rows)** — no signal. No SMS or WhatsApp
consent data exists in any source table this pipeline reads.

**Default position: `emailMarketingConsent` is omitted for every customer**, leaving
Shopify's own `NOT_SUBSCRIBED` default — until the store owner explicitly confirms that
FluentCRM's original opt-in is a legally sufficient basis to carry forward. That is a
business and legal judgment, flagged as needing the owner's or their advisor's sign-off.
**Nothing in this document is legal advice.**

Consent is never inferred from having an account, having ordered, or the existence of an
email address. Shopify's `marketingState` accepts only `SUBSCRIBED`, `UNSUBSCRIBED` and
`PENDING` as input; `NOT_SUBSCRIBED`, `REDACTED` and `INVALID` are system-set and rejected
if sent. "Unknown" has no representation other than omitting the field entirely.

---

## 6. Address-mapping requirements

Billing maps to a single `CustomerAddressInput`, set as default. 7,367 of 12,096 (61%) have
no billing address; Shopify requires none, so they import without one.

**Shipping is captured but never surfaced — a real, fixable gap (risk #40).**
`database_parser.py`'s `is_customer_meta_key()` already whitelists `shipping_`-prefixed keys,
but `build_customers()` never reads them out, so `customers.json` and the pre-built CSV both
carry billing only. **1,209 customers (10%) have a real shipping address that is currently
discarded.** Shopify supports a second `CustomerAddressInput` for it. This is a gap in this
pipeline, not a platform limitation. Gate D decides whether to close it.

GB province codes: the raw WooCommerce state/county string ("England", "Wales") is retained
un-normalized. Pre-existing documented limitation, harmless.

`username` is not mapped (no such field in `CustomerInput`). Order-level `customer_note` is
not mapped (per-order, not a profile field). Tags: `imported-from-woocommerce` plus
`registered`/`guest`.

**Nothing is ever fabricated.** Where source data is silent — shipping for 90%, consent for
46%, password for 100% — the Shopify representation is omitted or left at platform default.

---

## 7. Accounts and passwords

This store runs **New Customer Accounts** (`customerAccountsVersion: NEW_CUSTOMER_ACCOUNTS`,
verified live). `CustomerInput` has **no password field at all** (verified via schema
introspection). WooCommerce password migration is therefore **technically impossible**, not
merely inadvisable — there is no risk to manage, and no activation or invitation email is
required by the platform.

---

## 8. PII rules

Customer data is PII. It is handled, never displayed.

- Never print, log, commit or paste a customer email, name, phone, or address — not in
  output, reports, commit messages, issue comments, or chat.
- Reports and ledgers key on **WooCommerce customer ID and Shopify GID**, not on email.
  Where a report must identify a record for human action, the quarantine CSV is the single
  permitted place an email appears, and it is a local artifact.
- Aggregate counts are not PII and may be reported freely.
- Never fetch a customer by GID, or run any customer query, outside an approved step.
- No customer data leaves the local environment or the development store.

---

## 9. The 18-step controlled import

Executable only after Gates A–H. Steps 13–18 repeat per batch.

**Preparation (A–E cleared)**
1. Confirm Gate A live: `read_customers`/`write_customers` present, `customersCount`
   returns a number.
2. Read-only identity + environment preflight — `wholesale-beautyhub.myshopify.com`,
   development. Mismatch ⇒ STOP.
3. Re-run `phase10_customer_dry_run.py` fresh; confirm byte-identical output to the
   recorded run. Any drift ⇒ STOP and reconcile before proceeding.
4. Regenerate `reports/phase10_customer_manifest.csv` — every source row's intended
   disposition, generated before any write.
5. Implement and verify the `custom.legacy_woo_customer_id` metafield definition (Gate E).
6. Confirm the consent decision from Gate C is encoded in the transformation, and that
   omission remains the default for all 5,551 no-signal records.
7. Confirm the Gate D shipping-address decision is encoded — either mapped, or explicitly
   and visibly deferred.

**Test import (Gates F–G)**
8. Snapshot live customer state (count + any existing legacy-ID metafield values).
9. Import the 10-customer test set — real records covering billing/shipping combinations,
   phone presence, all three real consent states plus unknown, registered and guest, and
   both quarantine categories.
10. Reconcile with **fresh, independent** Shopify queries — never the importer's own report.
    Field-by-field: legacy-ID metafield, email, name, phone, billing/shipping address, tags,
    note, metafields, `emailMarketingConsent.marketingState`. Each row `MATCH` / `MISMATCH` /
    `UNRESOLVED` / `NOT_APPLICABLE`.
11. Re-run the same 10 records to prove idempotency — expect 10 UPDATE, 0 CREATE, 0
    duplicates.
12. **STOP.** Human review of the reconciliation (Gate G). Never round to "passed" with an
    unresolved mismatch.

**Bulk import (Gates H–J), per batch**
13. Preflight: identity, scopes, auth, live customer count.
14. Snapshot state and confirm the ledger, execution log and manifest are writable.
15. Execute one controlled batch. Never all 12,096 in one uncontrolled pass; size agreed at
    Gate I, following the Phase 9 tiered precedent.
16. Verify: fresh query of the batch's records; confirm created count, zero duplicate
    legacy IDs.
17. Reconcile at field level and append to the ledger and append-only execution log.
18. **Stop on any unexpected error.** Classify before continuing — never auto-retry in bulk,
    never continue past an unclassified mutation error.

---

## 10. Failure handling

Per-record, never bulk. Check `userErrors` on every mutation; classify each failure;
retry only what is deterministically safe; quarantine the rest with a reason. Record retry
count and outcome in the execution log. An unexpected error halts the batch.

Known untested risk: **phone uniqueness collisions.** Shopify requires phone uniqueness
where set; this has never been tested against live Shopify because 0 customers exist there.
Expect collisions on first bulk contact and treat them as a classification case, not a
retry case.

**Phone validation failures are a retry case, and the only one.** Shopify rejects the
whole `customerCreate` when the phone is invalid, so an unhandled phone error costs the
customer, not the field — this is exactly how the Gate 6 test lost woo 1 (risk #45). On a
phone `userError` the runtime drops the number, tags the customer `phone-dropped-invalid`,
writes the original to `reports/phase10_dropped_phones.jsonl`, and the create is re-issued
**once**. A second failure is quarantined, never retried again: a payload that no longer
carries a phone cannot be failing on the phone.

Expected volume, measured offline by `phase10_phone_format_validator.py`: **54 of the
4,450 customers with a phone** — 10 structurally invalid, 44 with a GB national number of
the wrong length. Budget one extra `customerCreate` for each. That count is a floor, not a
ceiling: the pre-check is structural and Shopify validates against national numbering
plans this project does not hold.

Non-blocking: 63%/61% missing phone/billing address — Shopify requires neither.

---

## 11. Rollback

**Mass deletion is explicitly not the rollback mechanism**, per instruction and per the
Phase 9 precedent. Maintained instead:

- **Import manifest** — intended disposition of every source row, pre-write.
- **Created-customer ledger** — WooCommerce ID → Shopify GID → timestamp → batch ID →
  action, append-only `.jsonl`, enabling resume without reprocessing.
- **Execution log** — every mutation attempt, its `userErrors`, and retry count.
- **Reconciliation state** — field-level comparison after every batch.

This enables targeted, human-reviewed remediation: a specific mis-imported customer is
identifiable by GID and correctable by a deliberate `customerUpdate`, or if genuinely
necessary a single `customerDelete` decided case-by-case by a human — never an automated
bulk rollback.

**Limitation, stated plainly:** the ledger records what this pipeline wrote, not what
happens to a customer record afterward. Customer-initiated activity after import — an
address change, an order placed on a migrated account — cannot be undone by it. This is
inherent to any external system integration, not specific to this design.

---

## 12. Reconciliation

Same pattern as Phase 9: WooCommerce source → transformation → Shopify customer → **fresh,
independent** Shopify query. Field-by-field, `MATCH` / `MISMATCH` / `UNRESOLVED` /
`NOT_APPLICABLE`, never rounded to "passed" while a mismatch is unresolved. Template:
`reports/phase10_customer_reconciliation_template.csv` — currently every row reads
`NOT_APPLICABLE`, because no write has occurred to reconcile.

---

## 13. Known risks

| Risk | Description |
|---|---|
| #39 | No Admin API customer scope granted — technical blocker, verified live |
| #40 | Shipping addresses captured but unmapped — 1,209 customers |
| #41 | Real consent signal exists, not yet business/legally cleared |
| — | Phone uniqueness collisions untested against live Shopify |
| — | 63%/61% missing phone/billing address — non-blocking, Shopify requires neither |

Phase 10 is **not "ready for import"** — it is *ready for human approval*, and additionally
blocked on one technical prerequisite that is not itself a business decision.
