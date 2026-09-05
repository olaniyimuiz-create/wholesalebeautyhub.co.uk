# Phase 10 — Current State (freeze record)

**Recorded 2026-08-22, before any bulk-importer code was written.**

> **NO SHOPIFY CUSTOMER MUTATIONS PERFORMED.**
> Every check below is a read or an offline computation. No customer, address,
> or metafield was created, updated, or deleted while producing this record.

---

## 1. Repository state

| | |
|---|---|
| Branch | `main` |
| HEAD | `d603a23ffda84bed34e4b85ba32b372025baf02a` |
| HEAD subject | *Harden Phase 10 live customer check* (2026-08-19) |
| `origin/main` | `5c546b0` — HEAD is **1 commit ahead, unpushed** |
| Working tree | **11 modified, 37 untracked** |

The working tree is **not clean, and that is the expected state**, not a
surprise: the whole of Phase 10 — the runtime, the province validator, the
readiness and review scripts, the decision matrix, ADR-014's signed gates — is
uncommitted work. Nothing has been committed since 2026-08-19 because no commit
has been authorized.

**No unexpected changes.** Every modified and untracked path belongs to Phase 10
or to the Phase 9 inventory-test hardening recorded earlier. Verified: the six
PII-bearing report files remain gitignored (manifest, quarantine, reconciliation
template, test cohort, name-conflict review, phone-collision review, address
exceptions, dropped phones, phone-format exceptions).

---

## 2. Approved manifest

| | |
|---|---|
| File | `reports/phase10_customer_manifest.csv` (gitignored — per-record PII) |
| SHA-256 | `2e31f3edbed607d3cec3bf2790ee9deae57e7c0e2f7dd07c14a0b1c4bcecdfda` |
| Rows | 13,043 |
| Classification | IMPORT 12,096 · SKIP 407 · QUARANTINE 539 · EXCLUDE 1 |
| `woo_customer_id` uniqueness | 12,096 IMPORT ids, all distinct |

12,096 + 407 + 539 + 1 = 13,043 ✓

**Run population: 11,849** — 12,096 IMPORT minus the 247 held back by ADR-014
Gate 5 (`EXCLUDE_AFFECTED_CUSTOMERS`). Held, not dropped: they import unchanged
once their names are confirmed.

---

## 3. Offline test suites

| Suite | Result |
|---|---|
| `test_phase10_import_runtime.py` | **644 / 644** |
| `tests/test_phase10_customer_import.py` (unittest discover) | **63 / 63** |
| `test_phase10_live_check.py` | **11 / 11** |
| `test_phase9_pricing.py` | **13 / 13** |
| **Total** | **731 assertions, 0 failures** |

`test_phase9_inventory.py` is excluded from routine runs: it performs real
`inventorySetQuantities` mutations and is opt-in behind
`PHASE9_ALLOW_LIVE_INVENTORY_WRITES=1`. It was **not** run.

---

## 4. Pre-flight (read-only)

`python migration/scripts/phase10_preflight.py` → **exit 0**, 15 passed, 0
failed, 0 gates open, **SHOPIFY MUTATIONS: 0**.

| Required confirmation | Result |
|---|---|
| Authentication | **PASS** — `Wholesale Beautyhub` (`wholesale-beautyhub.myshopify.com`) |
| Required scopes | **PASS** — 24 granted, incl. `read_customers` + `write_customers` |
| Development store | **PASS** — plan `Grow App Development`, `partnerDevelopment: true` |
| Customer count = 0 | **PASS** — 0 live customers |
| Customer metafield definitions | **PASS** — 0 defined, none required |
| Production-store protection | **PASS** — see below |
| API schema drift | PASS — 8 input types match the pinned 2026-07 contract |
| Province dataset | PASS — 756 codes / 22 countries |
| Mutation cost | PASS — 10 points/mutation, measured, 0 records created |

**Production-store protection**, stated precisely: what is verified today is
that the target store *is* a development store (`partnerDevelopment: true`,
confirmed live). The pre-flight reports that fact; it does not yet *refuse* a
non-development store. Enforcement is GUARD 1 and GUARD 2 of the bulk importer
and is built in this task, not before it.

---

## 5. Signed policies in force

| Policy | Decision | Gate |
|---|---|---|
| **Address** | `A_PLUS` — billing, else shipping, else none. At most one address per customer | Gate 2, 2026-08-22 |
| **Phone** | Evidence-scored recommendations stand; the 9 contested groups omit for every member. 76 send, 441 omit | Gate 1, 2026-08-22 |
| **Consent** | Approved — carry FluentCRM `subscribed` forward for 6,295. **Not applied by the import run**; a separate `customerEmailMarketingConsentUpdate` pass | Gate 3, 2026-08-22 |
| **Legacy ID** | `custom.legacy_woo_customer_id` **mandatory**, written inline in the same `customerCreate`. No parameter can omit it | Ratified 2026-08-21 |
| **Registered-at** | `custom.woo_registered_at` retained where the source has a date; never invented. `Customer.createdAt` never written | Ratified 2026-08-21 |
| **Missing email** | 292 records `PERMANENT_EXCLUSION` — no email fabricated | Gate 4, 2026-08-22 |
| **Names** | `EXCLUDE_AFFECTED_CUSTOMERS` — 247 held back, run population 11,849 | Gate 5, 2026-08-22 |
| **Architecture** | `customerCreate` → `customerAddressCreate`. `customerSet` out of scope | Ratified 2026-08-21 |

---

## 6. Credential status

| | |
|---|---|
| Token | present, `shpca_` prefix (custom app), length 38, sha256[:12] `9d3be75b7795` |
| Liveness | **live** — authenticated successfully at 2026-08-22 pre-flight |
| Scopes | 24, including `write_customers` |
| Expiry | **UNKNOWN** — risk register #44. Shopify custom-app tokens do not expire on a published schedule, and no expiry is exposed by the API |
| Handling | never printed, never logged, never written to a report — only prefix, length and hash |

The token grants `write_customers`. **That is capability, not authorization.**

---

## 7. Execution state

| | |
|---|---|
| Bulk customer migration | **never executed** |
| Live customers now | **0** |
| Prior live writes | Gate 6 test only — 9 created, 9 deleted, 2026-08-22; store verified back to 0 by listing records |
| Bulk importer | **did not exist** at the time of this freeze |
| ADR-014 Gate 7 | **requested, unsigned** |

---

> **NO SHOPIFY CUSTOMER MUTATIONS PERFORMED.**
