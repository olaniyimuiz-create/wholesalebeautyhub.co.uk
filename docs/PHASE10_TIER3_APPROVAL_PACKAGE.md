# Phase 10 — Tier-3 Approval Package

**Prepared 2026-08-22 under Approval A (BUILD ONLY).**

> ## NO TIER-3 TEST IS AUTHORIZED
>
> Approval A authorized *building* this mechanism. It authorized no execution of
> any kind. **Shopify mutations performed: 0.** The store holds 0 customers.
>
> Each test needs its own explicit approval, immediately before it runs, and no
> test's approval is inherited by the next.

---

## 1. Reviewed executor commit

| | |
|---|---|
| **Reviewed commit** | **`7987aa7e58e7942bad3a9eec737a87baffe6ca35`** |
| Branch | `phase10/gate7-executor-freeze` |
| Working tree | **clean** |
| Pushed | **no** |

`reviewed_commit()` returns the most recent commit touching the four files that
decide behaviour — the Tier-3 executor, the runtime, the province validator and
the frozen contract. **It is deliberately not HEAD.** An approval names this
value, and a later docs or report commit will not invalidate it, while any
change to those four files will.

Commit chain: `86f3211` (executor freeze) → `0494736` (Gate 7 evidence) →
`fead007` (Tier-3 executor) → `7987aa7` (approval pinning).

## 2. Frozen artefacts the executor pins to

| Artefact | SHA-256 |
|---|---|
| `migration/schema/phase10_migration_contract.json` | `d889fd03a0122ece7dcc1c741381a87cba90506eabe7d074608a6459ebc9feb0` |
| `reports/phase10_customer_manifest.csv` | `2e31f3edbed607d3cec3bf2790ee9deae57e7c0e2f7dd07c14a0b1c4bcecdfda` |

Either hash changing halts execution. Neither is edited to make a run pass.

## 3. What the executor can and cannot be pointed at

**Can:** three tests named in the file itself.
**Cannot:** an arbitrary customer id, a cohort file, the 11,849 manifest, or
anything supplied on the command line. No argument accepts a customer id, so
retargeting it is an edit to a committed file — a reviewable act, not a typo at
a prompt.

A second, independent bound refuses any definition holding more than 10
customers, so a definition edited into a cohort is rejected before anything
else looks at it.

## 4. The three tests

### TIER3-TEST-1 — ready for Approval B

| | |
|---|---|
| Customer | **woo 220** (registered) |
| Expected | **1 `customerCreate`, 0 `customerAddressCreate`** |
| Metafields | `custom.legacy_woo_customer_id` = `220`, `custom.woo_registered_at` |
| Phone | **sent** — no collision, not flagged by the format pre-check |
| Consent | **absent** — no code path in this module sets it |
| Address | none — the customer has none in source |
| Payload fields | `email`, `firstName`, `lastName`, `phone`, `tags`, `metafields` — and nothing else |
| Store precondition | 0 customers |
| Rollback | specified, **not executable** |

**Simulated 2026-08-22**, 0 mutations →
`reports/phase10_tier3_simulation_tier3_test_1.json`.

Approval phrase, exact:

```
APPROVED - EXECUTE TIER-3 TEST 1 FOR WOO CUSTOMER 220
```

Command it would authorize:

```
python migration/scripts/phase10_tier3_executor.py --execute TIER3-TEST-1 \
  --authorization "APPROVED - EXECUTE TIER-3 TEST 1 FOR WOO CUSTOMER 220" \
  --expect-commit 7987aa7e58e7942bad3a9eec737a87baffe6ca35
```

### TIER3-TEST-2 — prepared, NOT authorized

**woo 2** — 1 create + 1 address, GB, `provinceCode` **omitted** despite a county
in source, postcode preserved exactly, `setAsDefault: true`, 1 metafield (guest,
no registration date). Address fields sent: `address1`, `city`, `countryCode`,
`firstName`, `lastName`, `phone`, `zip`.

**Simulated 2026-08-22**, 0 mutations →
`reports/phase10_tier3_simulation_tier3_test_2.json`.

Deliberately **not woo 1**, which was the lowest-id match: woo 1 is the customer
the Gate 6 run lost to `Phone is invalid`, so using it here would put two
variables in one test.

### TIER3-TEST-3 — defined, cohort NOT FROZEN

The executor **refuses to run it**, and will keep refusing until a cohort is
approved. No approved 10-customer Tier-3 cohort exists, and inventing one here
would be manufacturing the approval this module exists to require.

Recorded requirements for whoever freezes it:

* **woo 1 must be a member** — the only case that exercises the risk #45 phone
  fallback against live Shopify, which has never happened.
* A cohort containing a **shipping-fallback** customer **cannot be built from the
  manifest** — the manifest carries no shipping postcode or province. Such a
  cohort must be derived from source.

Freezing the cohort is its own decision, separate from authorizing the test.

## 5. Guards enforced before any mutation

| Requirement | How |
|---|---|
| 1–5 | Only defined test ids; no customer id, cohort file or manifest is accepted; a >10 definition is refused |
| 6 | No generic `--mode live`; execution names one test |
| 7–10 | The definition must exist and its expected creates, addresses, metafields and phone outcome must match the built payload |
| 11 | Frozen contract sha256 must match |
| 12 | `--expect-commit` must equal `reviewed_commit()`, and the tree must be clean |
| 13, 16, 17 | Development store required; `partnerDevelopment` missing is treated as production; production domain markers refused |
| 14 | Test 1 requires `customersCount == 0` |
| 15, 19 | Read-only pre-flight; `read_customers` + `write_customers` required |
| 18 | API version must be `2026-07` |
| 20 | `CustomerInput` gaining `addresses`, or losing a required field, halts |
| 21–22 | The legacy id must be absent from the live store; a duplicate in a definition halts |
| 23–25 | Token never printed or logged; ledger schema rejects PII; `userErrors` sanitized before writing |

Authorization is checked **before the store is touched at all** — a test with a
wrong phrase sends zero documents, which is asserted by test.

## 6. Retry, throttle and failure policy

Every decision is the runtime's: pacing from `extensions.cost`, response and
exception classification, backoff `1/2/4/8/16` with proportional jitter,
`MAX_TRANSIENT_ATTEMPTS = 3`, throttle retries not counted as failures.

**One design point worth stating plainly.**
`phase10_import_runtime.execute_with_retry` refuses mutation documents by
construction — the runtime is read-only and that guarantee was **not** weakened
for Tier-3. So `send_mutation()` keeps the loop local and imports every
decision. No schedule, no jitter and no classification is defined in the
executor, and a test asserts the runtime still refuses the executor's own
`CUSTOMER_CREATE` document.

* **401 / `ACCESS_DENIED`** → halt immediately, never retried.
* **Ambiguous timeout** → verify by legacy id **before** retrying. If the write
  landed, the run halts for reconciliation rather than creating a duplicate.
* **Address failure** → the customer survives; the address is retryable on its
  own and is never fixed by recreating the customer.

## 7. Rollback

`customerDelete` has a document and **no executable path**. `rollback_spec()`
describes what a rollback would do — target GIDs, and the precondition that the
legacy metafield is re-read and matched before any delete. `execute_rollback()`
raises `RollbackNotAuthorized`.

The document is present so a reviewer can see exactly what would be sent, and
unreachable so nothing sends it. Rollback is a Shopify mutation like any other
and needs its own explicit authorization.

## 8. Testing

**142 offline tests, all passing, all mocked.** No credential is read, no socket
is opened, and no Shopify mutation is sent. All customer data in the suite is
invented — `example.com` addresses, Ofcom `07700 900xxx` numbers, made-up
streets.

Full regression: **142** unittest · **644** runtime · **11** live-check · **13**
Phase 9 pricing = **810 assertions, 0 failures**.

```
SHOPIFY MUTATIONS:  0
CUSTOMER WRITES:    0
ADDRESS WRITES:     0
METAFIELD WRITES:   0
```

## 9. Residual risks

| Risk | Position |
|---|---|
| The executor can write, and previous Phase 10 modules could not | Contained by per-test authorization, a frozen target list, and simulation as the default mode |
| Tier-3 has never run against live Shopify | That is what Approval B is for. The risk #45 phone fallback in particular has never been exercised live |
| Test 3's cohort is not frozen | The executor refuses the test. Freezing it is a separate decision |
| Token lifetime unknown (risk #44) | Detect and halt on 401; no prediction attempted |
| Branch not pushed | Deliberate. Pushing is a separate authorization |

## 10. Determination

**Tier-3 executor: BUILT and VALIDATED OFFLINE. No test executed.**

The next thing that may happen is not an execution. It is a decision:

```
Approval B — Execute Tier-3 Test 1 for Woo customer 220.
```
