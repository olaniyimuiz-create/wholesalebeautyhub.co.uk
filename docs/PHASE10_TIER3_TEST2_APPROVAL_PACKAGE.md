# Phase 10 — Tier-3 Test 2 Approval Package

**Prepared 2026-08-23. Test 2 is NOT authorized and has NOT been executed.**

> Test 1 passed and its customer is live and untouched. That authorizes nothing
> here: Tier-3 approval is per test and is never inherited.

---

## 0. What changed since Test 1 — read this first

The auditability defect found in the Test-1 review is fixed, and **fixing it
moved the reviewed behavioural commit**. Any Test-2 authorization must pin the
new one:

| | |
|---|---|
| Reviewed commit at Test 1 | `7987aa7e58e7942bad3a9eec737a87baffe6ca35` |
| Auditability correction | `cb53ab475f4df2224142b493f0eeee72e44ac794` |
| **Reviewed commit for Test 2** | **`cb8abd7069a60d65a11825a83e4af85906825f67`** |

`--expect-commit 7987aa7…` or `cb53ab4…` will now **halt**, correctly — something
that decides behaviour has changed since those approvals were given.

**Amended 2026-08-23 under Approval A**: the empty-store blocker described in §9
of the previous revision is resolved. Test 2 now requires an exact pre-test
customer count of **1**, and requires that customer to be **verifiably woo 220**
by its legacy metafield. That is stricter than the flag it replaces.

## 1. Source identity

| | |
|---|---|
| Woo customer id | **2** |
| Account type | guest (no WordPress account, hence no registration date) |
| In the approved run cohort | yes |
| Deferred by Gate 5 (name conflict) | **no** |
| Phone collision group | **none** — woo 2 shares its number with nobody |
| Manifest | `reports/phase10_customer_manifest.csv`, sha256 `2e31f3ed…cdfda` |
| Contract | `migration/schema/phase10_migration_contract.json`, sha256 `d889fd03…9feb0` |

**Independently recalculated from the source dump**, not read back from an
earlier report: the payload derived from `dump.sql` and the payload derived from
the hash-verified manifest are **identical across all 14 compared properties**.

No email, name, phone value, street or postcode appears in this document.

## 2. Expected customer payload

`customerCreate(input: CustomerInput!)` — exactly these fields:

```
email, firstName, lastName, phone, tags, metafields
```

| Property | Value |
|---|---|
| tags | `imported-from-woocommerce`, `guest` |
| metafields | **1** — `custom.legacy_woo_customer_id = "2"` |
| `woo_registered_at` | **absent** — woo 2 is a guest with no registration event, and a date is never invented |
| `emailMarketingConsent` | **absent** |
| `company` | **absent from the customer** — company belongs on the address |
| password / username / capabilities | absent; the fields do not exist on `CustomerInput` |

Contrast with Test 1, which carried **2** metafields because woo 220 is a
registered account with a real registration date. One metafield here is correct,
not a regression.

## 3. Expected address payload

`customerAddressCreate(customerId:, address: MailingAddressInput!, setAsDefault:)`
— issued **only after** `customerCreate` returns a customer id.

| Property | Value |
|---|---|
| kind | billing |
| fields sent | `address1`, `city`, `countryCode`, `firstName`, `lastName`, `phone`, `zip` |
| `countryCode` | **GB** |
| `provinceCode` | **OMITTED** |
| `setAsDefault` | `true` |
| postcode | **preserved exactly**, trimmed only — verified `zip == source.strip()`; shape `AA## #AA` |

**Why `provinceCode` is omitted although the source has a county.** The source
county is present, and it is dropped deliberately: Shopify's five GB zones are
`ENG`/`NIR`/`SCT`/`WLS`/`BFP`, while WooCommerce stores counties such as
"Surrey". A county is never one of those codes, so the ratified rule omits
`provinceCode` for GB unconditionally. The county is **preserved in the source**
and simply not sent — nothing is rewritten, coerced, or guessed.

## 4. Expected mutation count

| | |
|---|---|
| `customerCreate` | **1** |
| `customerAddressCreate` | **1** |
| metafield mutations as separate calls | **0** — the one metafield rides inline |
| `customerDelete` / rollback | **0** |
| **Total** | **2** |

Store expected to hold **1** customer before Test 2 (the Test-1 customer) and
**2** after.

**Note a difference from Test 1**: Test 1 required an empty store. Test 2's
definition sets `requires_store_empty = True` as well, so **the pre-flight will
halt** on the Test-1 customer still being present. See §9 — this needs a
decision before Test 2 can run.

## 5. Expected metafields

```
custom.legacy_woo_customer_id = "2"    single_line_text_field, inline
```

No second metafield. No metafield definition is created — none is required.

## 6. Phone handling

Sent. Woo 2 is in no collision group, so the collision policy does not omit it,
and the offline format pre-check does not flag it.

**Expect the live value to differ from the string sent.** Shopify normalises to
E.164, exactly as observed in Test 1 and the Gate 6 cohort. Verification must
compare canonical forms; a raw string comparison will report a false mismatch.

## 7. Province handling

`GB → provinceCode omitted`, unconditionally and by ratified rule. Verified 0
GB province codes across the whole 11,849 cohort.

## 8. Postcode handling

Sent verbatim with surrounding whitespace trimmed and nothing else. The executor
**halts** if the value it is about to send is anything other than the trimmed
source value.

## 9. Pre-flight requirements

| Requirement | Status |
|---|---|
| Authentication | must PASS |
| API version | `2026-07` |
| Scopes | `read_customers` + `write_customers` |
| Development store | `partnerDevelopment: true`; missing information is treated as production |
| Schema | `CustomerInput` must still have no `addresses` field |
| Contract sha256 | `d889fd03…9feb0` |
| `--expect-commit` | `cb8abd70…` and a **clean working tree** |
| Legacy id 2 absent from the live store | required |
| **Customer count** | **exactly 1** — asserted, and the store holds 1 |
| **Pre-existing identity** | **woo 220 must be verifiably present** by legacy metafield |

**RESOLVED 2026-08-23 under Approval A.** `requires_store_empty` was the
`Tier3Test` class default, inherited by a test designed to run *after* Test 1.
It is replaced by an explicit two-part invariant:

```
expected_customer_count       = 1        exact; 0, 2, 3+ all halt
expected_preexisting_woo_ids  = (220,)   verified by legacy metafield
```

Both halves run inside `preflight()`, **before any mutation**. The identity half
is why both are needed: a store holding exactly one customer satisfies
`count == 1` whether that customer is the Test-1 record or something nobody
authorized, and only the metafield lookup distinguishes them.

Covered by 16 tests: counts 0/1/2/3+, an unexpected customer, the Test-1
customer plus an intruder, the correct count with an unverifiable identity, and
the correct state proceeding — every one offline, with a mock that raises if a
mutation is attempted.

## 10. Reviewed executor commit

**`cb8abd7069a60d65a11825a83e4af85906825f67`**

Behavioural files at this commit:

```
phase10_tier3_executor.py        changed - store-state guard (this amendment)
phase10_import_runtime.py        unchanged since 7987aa7
phase10_province_validator.py    unchanged since 7987aa7
phase10_migration_contract.json  unchanged since 7987aa7
```

## 11. Rollback specification

If Test 2 runs and rollback is later authorized, it would be `customerDelete`
per record, driven by the ledger, each delete **re-reading the customer and
confirming `custom.legacy_woo_customer_id` matches before removing it**. The
address is removed with its customer; there is no separate address delete.

## 12. Rollback is NOT authorized

`customerDelete` has a document and **no executable path**.
`execute_rollback()` raises `RollbackNotAuthorized`. This applies to the Test-1
customer as well, which remains live and unmodified.

## 13. Exact command that would execute Test 2

**Do not run this until §9 is resolved and Test 2 is explicitly authorized.**

```
python migration/scripts/phase10_tier3_executor.py --execute TIER3-TEST-2 \
  --authorization "APPROVED - EXECUTE TIER-3 TEST 2 FOR WOO CUSTOMER 2" \
  --expect-commit cb53ab475f4df2224142b493f0eeee72e44ac794
```

## 14. Required authorization phrase

```
APPROVED - EXECUTE TIER-3 TEST 2 FOR WOO CUSTOMER 2
```

Exact string, no paraphrase. Test 1's phrase is rejected for Test 2 by name, and
that rejection is covered by test.
