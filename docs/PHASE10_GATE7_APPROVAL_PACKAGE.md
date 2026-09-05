# Phase 10 — Gate 7 Approval Package

**Prepared 2026-08-22.** Bulk customer import into
`wholesale-beautyhub.myshopify.com`.

> ## GATE 7 STATUS: **BLOCKED**
>
> Not blocked on a decision — every business gate is signed and every figure
> reconciles exactly. Blocked on **evidence that does not yet exist** and on
> **code that has not yet been written**:
>
> 1. **Tier-3 live tests have not been executed.** They are not authorized, and
>    this package does not request them as a batch: each of the three requires
>    its own explicit approval immediately before it runs.
> 2. **The live executor does not exist.** `phase10_bulk_import.py` is dry-run
>    only by construction — no mutation document, no transport.
> 3. ~~The importer is uncommitted.~~ **RESOLVED 2026-08-22** - frozen at commit
>    `86f3211`, and the dry-run evidence was regenerated against it.
>
> **NO SHOPIFY CUSTOMER MUTATIONS HAVE BEEN PERFORMED.**

---

## 1. Current Shopify state

| | |
|---|---|
| Store | `wholesale-beautyhub.myshopify.com` — *Wholesale Beautyhub* |
| Plan | Grow — **`partnerDevelopment: true`** (development store) |
| **Customers** | **0** |
| Products | 596 (Phase 9) |
| Customer metafield definitions | 0 — none required |
| Scopes | 24, including `read_customers` + `write_customers` |
| Pre-flight | **exit 0**, 15 passed, 0 failed, 0 gates open, 0 mutations |

Prior live customer writes: the Gate 6 test only — 9 created, 9 deleted on
2026-08-22, store verified back to 0 **by listing records**, not by trusting a
count that had already proved eventually-consistent.

## 2. Current Git commit

| | |
|---|---|
| Branch | `phase10/gate7-executor-freeze` (branched from `main` at `d603a23`) |
| HEAD | `86f3211e58a1eb69bac4ac47ccc436fd3ed17a68` |
| `origin/main` | `5c546b0` — **not pushed**; pushing requires separate authorization |
| Working tree | clean apart from regenerated evidence, committed separately |

## 3. Exact importer commit

**`86f3211e58a1eb69bac4ac47ccc436fd3ed17a68`** (short `86f3211`).

Frozen 2026-08-22 on branch `phase10/gate7-executor-freeze`. It contains the
runtime, the dry-run bulk importer, the verifier, the frozen contract, the
province and phone validators, ADR-014 with Gates 1-6 signed, and 758 passing
offline assertions.

The evidence was then **regenerated against that commit**, so
`reports/phase10_bulk_import_dry_run.json` now stamps
`importer_commit: 86f3211` — the commit that actually produced it, not the tree
it happened to sit in. Evidence is committed separately from code so that the
importer commit stays the answer to "what would run?".

**Not pushed.** `origin/main` is still at `5c546b0`. Pushing is a separate
authorization.

## 4. Manifest hash

| | |
|---|---|
| File | `reports/phase10_customer_manifest.csv` (gitignored — per-record PII) |
| SHA-256 | `2e31f3edbed607d3cec3bf2790ee9deae57e7c0e2f7dd07c14a0b1c4bcecdfda` |
| Rows | 13,043 — IMPORT 12,096 · SKIP 407 · QUARANTINE 539 · EXCLUDE 1 |

GUARD 6 refuses to proceed if this hash changes. GUARD 7 additionally compares
the manifest's 12,096 ids **as a set** against the ids the source classifies
to today — a count check alone would pass two different populations of the same
size.

## 5. Exact approved population

**11,849.**

```
13,043  source rows
   -407  duplicate SKIP
   -539  QUARANTINE  (of which 292 are the missing-email PERMANENT_EXCLUSION)
     -1  EXCLUDE
 12,096  IMPORT  (approved manifest)
   -247  deferred by Gate 5 (EXCLUDE_AFFECTED_CUSTOMERS)
 11,849  RUN POPULATION
```

The 247 are **held, not dropped** — they import unchanged once a reviewer
confirms their names.

## 6. Exact mutation count

| | |
|---|---|
| `customerCreate` | **11,849** |
| `customerAddressCreate` | **4,521** |
| **Total** | **16,370** |
| Expected phone-fallback retries | 51 |
| Worst case | **16,421** |
| Expected throttles | **0** |
| Cost | 163,700 points at the measured 10/mutation |
| Duration | **~27 minutes** at the measured sustained 10 mutations/s |

Zero throttles is arithmetic, not optimism: the bucket restores 100 points/s and
a mutation costs 10, so pacing at 10/s spends exactly what is restored. Any
throttle that does occur is absorbed by the tested backoff and does **not**
consume the transient-failure budget.

## 7. Phone collision resolution

| | |
|---|---|
| Collision groups | 240, covering 517 IMPORT customers |
| Resolution | 76 `KEEP_ONE` · 155 `OMIT_FROM_ALL` · 9 contested |
| In-cohort outcome | **3,799 send · 428 omit · 7,622 have no phone** = 11,849 ✓ |
| Contested groups | 19 customers hold at `OMIT_PHONE_PENDING_REVIEW` |

**Verified invariant: no two customers in the cohort would send the same phone
number.** Computed by canonicalising every phone that survives the policy and
checking for duplicates — 0 found. This is the check that matters, because
Shopify enforces store-wide phone uniqueness and the second write is the one
that fails.

The 9 contested groups were **not** resolved by picking the first customer. They
omit for every member. That is Gate 1's signed decision, and it is deliberately
the safe outcome rather than a guess about whose number it is.

## 8. Address policy

**`A_PLUS` — billing, else shipping, else none. At most one address per customer.**

| | |
|---|---|
| Billing address | 4,505 |
| Shipping fallback | 16 |
| No address | 7,328 |
| **Total** | **11,849** ✓ |

Verified across the full cohort: **0** GB addresses carry a `provinceCode`,
**0** invalid or raw province strings are sent, **0** country codes are invented,
**0** postcodes are rewritten (trimmed only), and **0** customers are blocked by
an address problem.

## 9. Consent policy

**Approved (Gate 3) — and deliberately not applied by this run.**

`emailMarketingConsent` is absent from every payload, and its absence is
asserted before send. 6,065 in-cohort records carry FluentCRM `subscribed` and
can be set afterwards with `customerEmailMarketingConsentUpdate`, which needs no
re-import. Consent therefore does not gate the import order, and a failed import
cannot half-apply consent.

## 10. Legacy-ID policy

`custom.legacy_woo_customer_id` — `single_line_text_field`, the Woo id as a
string, written **inline in the same `customerCreate`**, never as a follow-up.

**11,849 of 11,849** payloads carry it. There is no parameter that could omit
it, `assert_legacy_metafield_present()` raises before any send, and GUARD 14
checks it again per record. A customer created without it is unmatchable,
unskippable on resume, and unfindable for rollback.

## 11. Retry / backoff policy

| | |
|---|---|
| Schedule | 1, 2, 4, 8, 16 seconds, +up to 25% proportional jitter, clamped at 16 |
| Transient failures | 3 attempts, then the record is quarantined |
| Throttling | retried up to 50 times and **never** counted as a failure |
| Phone `userError` | drop the phone, tag `phone-dropped-invalid`, retry **once** |

Throttling and failure are kept apart on purpose: conflating them quarantines
healthy records under load, which is exactly when quarantining is most expensive
to unpick.

## 12. Timeout policy

**Verify before retry.** A timeout is genuinely ambiguous — the mutation may
have committed before the connection dropped — so the runtime asks the server
whether the write landed before re-sending. This is what stops a retry from
creating a duplicate customer.

## 13. Token-expiry policy

**A 401 or `ACCESS_DENIED` halts the run immediately and is never retried.**
Every subsequent record would fail for a reason that has nothing to do with the
record, and a run that keeps going would produce thousands of misleading
quarantine entries.

Token expiry itself is **UNKNOWN** (risk #44): Shopify publishes no expiry
schedule for custom-app tokens and none is exposed by the API. The mitigation is
detection and immediate halt, not prediction.

## 14. Rollback procedure

1. The ledger holds every created GID against its Woo id, flushed and fsynced
   per record.
2. Rollback is `customerDelete` **per record, driven by the ledger**, each one
   re-verifying the legacy metafield before deleting — the Gate 6 test used
   exactly this path for all 9.
3. **Mass deletion is explicitly not the rollback mechanism** (see
   `PHASE10_IMPORT_PROCEDURE.md` §11).
4. The window closes at go-live. After that a created customer is a real
   customer and deletion is a business decision, not a technical one.

## 15. Reconciliation procedure

* **The Shopify legacy-ID map is the source of truth**, not the checkpoint. The
  checkpoint is a resume accelerator; if the two disagree, Shopify wins.
* On resume, `fetch_existing_legacy_map()` pages every customer and their
  addresses in one scan — a single pass, not a per-record lookup.
* Post-run: field-by-field comparison of planned vs live for every record,
  comparing phones in **canonical** form because Shopify normalises to E.164
  (this produced 4 false mismatches in the Gate 6 test until it was fixed).
* `partial_address_customers()` identifies customers created without their
  planned address so addresses can be retried independently, without touching
  the customer.

## 16. Tier-3 test results

**NOT STARTED. Not authorized. Not executed.**

The three tests are specified and none may be inferred from another:

| Test | Scope | Status |
|---|---|---|
| TEST 1 | 1 customer, no address | **awaiting explicit approval** |
| TEST 2 | 1 customer with an approved address | **awaiting explicit approval** |
| TEST 3 | 10 mixed — resume, duplicates, checkpoint, fallback, timeout, throttle | **awaiting explicit approval** |

The earlier Gate 6 test (9 created, 9 deleted, 117 field checks, 0 real
mismatches) is **evidence, not a substitute**: it predates the phone fallback,
the A_PLUS address policy, and this importer.

## 17. Remaining risks

| # | Risk | Severity | Position |
|---|---|---|---|
| — | **Live executor not built** | **Blocking** | Dry-run-only by construction; the write path is a separate, reviewable change |
| — | **Importer uncommitted** | **Blocking** | Commit and record the hash before any bulk run |
| — | **Tier-3 not executed** | **Blocking** | Three tests, three separate approvals |
| 45 | ~51 customers lose their phone number | High → mitigated | Retry-without-phone implemented and tested; customer never lost. Figure is a **floor** — the pre-check is structural |
| 44 | Token lifetime unknown | Medium | Detect and halt; no prediction attempted |
| 43 | Province codes not cross-checked live | Low | Accepted limitation. Needs `read_shipping`/`read_markets`, deliberately not requested |
| — | 19 customers keep no phone | Low | Gate 1's signed safe default; addable later with `customerUpdate` |
| — | 247 customers not imported | Low | Gate 5; held, not dropped |
| — | 15 customers import with no address | Low | Source has no country; GB is never assumed |
| — | Consent approved on an unverified basis | Recorded | This pipeline never saw how FluentCRM collected opt-in; the owner's judgment, recorded with its limits |

## 18. Gate 7 determination

**BLOCKED.**

Engineering is complete and verified — 20/20 reconciliation checks, 90 offline
tests, pre-flight exit 0, every figure recomputed from source and matching the
dry run exactly. What is missing is not analysis. It is Tier-3 evidence, a live
executor, and a commit.

When those exist, this package is updated and the determination becomes READY
FOR APPROVAL. Only then can the store owner give the one thing that authorizes a
bulk run:

```
APPROVED - EXECUTE GATE 7 BULK CUSTOMER IMPORT
```

No paraphrase, no previous approval, and no engineering recommendation is a
substitute. `phase10_bulk_import.py` GUARD 5 accepts that exact string and
nothing else — and in this revision, even that is refused, because the write
path does not exist.
