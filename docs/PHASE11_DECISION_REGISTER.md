# Phase 11 — Decision Register

**Decisions required before any order import. Nothing here is approved.
Shopify mutations performed: 0.**

Each entry carries measured evidence, the available options, the consequence of
each, and an engineer's **recommendation**. A recommendation is not a decision
and never becomes one by default or by silence.

Labels: **VERIFIED** · **INFERRED** · **UNKNOWN** · **REQUIRES BUSINESS
DECISION** · **REQUIRES SHOPIFY ACCESS/SCOPE**

---

## Summary of exposure

| Class | Orders | Value | Root cause known? |
|---|---:|---:|---|
| `IMPORT_READY` | 7,780 | £439,842.59 | n/a |
| `MISSING_PRODUCT` | 262 | £24,164.65 | **YES** — 16 products |
| `MISSING_CUSTOMER` | 126 | £8,449.97 | **YES** — 3 causes |
| `MISSING_VARIANT` | 58 | £5,480.19 | **YES** |
| `FINANCIAL_MISMATCH` | 22 | £1,085.00 | **YES** — source defect |
| `PAYMENT_AMBIGUOUS` | 9 | £194.03 | partial |
| `REFUND_COMPLEX` | 4 | £95.16 | **YES** |
| `UNSUPPORTED_CURRENCY` | 4 | £323.10 | **YES** |
| `MISSING_ORDER_DATA` | 2 | £43.00 | **YES** — no line items |

Root-cause analysis reduced the exception surface substantially: the 262
`MISSING_PRODUCT` orders trace to just **16 distinct products**, and the 58
`MISSING_VARIANT` orders are mostly a side-effect of those same products.

---

## D-1 — Migrate non-completed orders? **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 554 orders, £31,309.29 — `wc-failed` 423 (£23,803.62),
`wc-cancelled` 122 (£7,462.67), `wc-pending` 7, `wc-processing` 6,
`wc-checkout-draft` 2 (£43.00, and these are abandoned carts).

| Option | Consequence |
|---|---|
| **A** Migrate completed + refunded only (7,649) | Cleanest revenue picture. Loses failure/cancellation history |
| **B** Migrate all 8,209 | Complete history. 423 failed orders appear as real orders; Shopify reporting may count them |
| **C** Migrate all except `checkout-draft` (8,207) | Keeps genuine commercial events, drops abandoned carts |

**Recommendation: C.** `wc-checkout-draft` orders are carts the customer never
submitted; importing them creates records that never existed commercially.
Failed and cancelled orders *are* real events worth retaining, mapped to
`VOIDED` so they never read as revenue.

---

## D-2 — Guest orders: attach or leave unlinked? **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 6,735 of 8,209 orders (82%) are guest checkouts with
`customer_id = 0`. Every one has a billing email.

| Option | Consequence |
|---|---|
| **A** Import with `email` only, no customer link | Faithful to source. Order history not visible on any customer account |
| **B** Match guest email to a Phase 10 customer and associate | Richer customer histories. Asserts an identity link WooCommerce never made |
| **C** Associate only on exact email match, else unlinked | Middle path; still an inference, but an evidenced one |

**Recommendation: A, unless the business specifically wants guest history on
accounts.** Option B invents a relationship the source did not record. If B or C
is chosen, `customer.toAssociate` must be used — **never `toUpsert`**, which
would create customers outside the Phase 10 ledger.

---

## D-3 — 126 orders with unmapped customers, and an upstream Phase 10 gap

**This is the most consequential finding in the package. VERIFIED.**

Root cause of the 126 orders:

| Cause | Orders | Distinct customers |
|---|---:|---:|
| **Customer eligible but never attempted by Phase 10** | **104** | **73** |
| Phase 10 `QUARANTINE` | 21 | 15 |
| Phase 10 `FAILED` (invalid email) | 1 | 1 |

Tracing the first row upstream:

- Phase 10 manifest classifies **12,096** customers as `IMPORT`
- The Phase 10 executor attempted **11,849**
- **247 `IMPORT`-classified customers were never attempted at all**
- Only 5 in-ledger records lack a GID (the known `FAILED` set)

247 is exactly the size of the `duplicate_email_conflicting_identity` quarantine
class, which strongly suggests the 11,849 population was deliberately defined by
removing them. **But those 247 are classified `IMPORT`, not `QUARANTINE`, in the
manifest.** Whether the exclusion was a deliberate, documented scope decision or
an unnoticed gap is **UNKNOWN** — it cannot be determined from the artifacts I
have read, and I will not assume it.

**Separate audit observation, offered neutrally:**
`phase10_bulk_import_executor_plan.json` records
`"authorization": "ADR-014 Gate 7 is UNSIGNED. This is a plan, not permission."`
while `phase10_bulk_import_executor_result.json` records `"mode": "execute"` with
16,492 mutations performed. The most likely explanation is that the gate was
signed after the plan was generated and the plan file was simply never
regenerated. **This should be confirmed by whoever signed Gate 7**, and is
Phase 10 governance, not a Phase 11 blocker.

**Required before D-3 can be answered:** confirm whether the 247 exclusion was
intended. If it was not, Phase 10 has a completeness gap that should be closed
before Phase 11 imports orders referencing those customers.

**Recommendation:** resolve the 247 question first, then treat any still-unmapped
order as guest-style (email only) rather than blocking it.

---

## D-4 — 262 orders referencing products not in Shopify **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** just **16 distinct products** cause all 262 orders:

| Cause | Products | Line items | Line value |
|---|---:|---:|---:|
| Deleted from WooCommerce, never in the 611-row catalogue | 11 | 204 | £2,969.13 |
| Phase 9 `QUARANTINE` (no price / placeholder vendor) | 5 | 126 | £9,889.57 |

**A capability confirmed by introspection changes this decision (VERIFIED):**
`OrderCreateLineItemInput.productId` is documented as nullable — *"Can be `null`
if the original product associated with the order is deleted."* With `title`,
`sku` and `priceSet` supplied, the line item imports faithfully as an unlinked
historical record.

| Option | Consequence |
|---|---|
| **A** Import with `productId: null`, keeping title/sku/price | Financial history complete and accurate; line not clickable to a product |
| **B** Import the 5 quarantined products into Shopify first | Fixes 126 line items, but those products are quarantined for real reasons (no price, placeholder vendor) |
| **C** Quarantine all 262 orders | Loses £24,164.65 of order history |

**Recommendation: A for the 11 deleted products** — Shopify explicitly supports
it. **For the 5 quarantined products, A as well**, since importing a product with
no real price purely to satisfy an order line would reintroduce exactly the data
defect Phase 9 quarantined it for.

---

## D-5 — 58 orders with unmapped variants **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 69 line items where the parent product is itself
unmapped (resolved by D-4), and **12 line items where the parent product *is*
live but the variation is missing from the Phase 9 variant mapping**. Those 12
are the only genuinely novel cases.

**Recommendation:** follow D-4 — supply `productId` where known, omit
`variantId`, and carry `variantTitle` as text. Investigate the 12 before import;
they may be the same broken-variation class Phase 9 quarantined (10966, 19990).

---

## D-6 — 4 non-GBP orders **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** EUR 3 (£307.10 equivalent as stored), USD 1 (16.00).
Store currency is GBP.

**Recommendation: MANUAL_REVIEW.** Four orders worth £323.10 do not justify
building currency handling, and **no FX rate will be invented**.

---

## D-7 — 22 orders with pre-existing financial variance **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 22 orders where WooCommerce's own stored total disagrees
with its components by −0.71 to +0.29 (order 2289: components 13.09, stored
13.00). Total exposure £1,085.00. **This is a source-data defect that predates
the migration.**

| Option | Consequence |
|---|---|
| **A** Import with source components, accept the declared variance | History preserved; reconciliation shows 22 known variances forever |
| **B** Quarantine all 22 | 22 real orders missing from history |
| **C** Adjust values to force reconciliation | **Rejected** — fabricates financial data |

**Recommendation: A**, with each variance recorded explicitly in the
reconciliation ledger. C must not be chosen under any circumstance.

---

## D-8 — Discount representation **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 27 orders carry a coupon; total discount across the
entire order history is **£35.20**. `OrderCreateOrderInput` exposes a single
`discountCode`.

**Recommendation:** accept order-level representation. At £35.20 total exposure,
building multi-coupon fidelity is not a defensible use of effort.

---

## D-9 — Refund fidelity — **RESOLVED TECHNICALLY, confirmation only**

Previously the largest open risk. **Now VERIFIED:** `RefundInput.processedAt`
exists, so refunds *can* be back-dated to their original WooCommerce date, and
`RefundInput.notify` allows suppression.

146 refunds: 52 full, 86 partial, 4 multiple (£4,827.15). The 4 multiple-refund
orders remain **MANUAL_REVIEW**.

**Recommendation:** back-date all refunds via `processedAt`, `notify: false`,
and hold the 4 complex cases for human confirmation.

---

## D-10 — Non-ISO country values **REQUIRES BUSINESS DECISION + investigation**

**Evidence (VERIFIED that the defect exists):** distinct billing country values
include `Re` and `Un`. A valid 2-character ISO code truncated to 2 characters is
unchanged, so these prove longer non-ISO strings are stored. Shopify requires ISO
codes. **Exact count and full value set are UNKNOWN** — one targeted pass is
needed.

**Recommendation:** run the targeted pass, map values that are unambiguous (e.g.
"United Kingdom" → `GB`), and send anything ambiguous to `MANUAL_REVIEW`. No
country will be guessed.

---

## D-11 — VAT-exempt flag **REQUIRES BUSINESS DECISION**

**Evidence (VERIFIED):** 8,168 of 8,209 orders carry `is_vat_exempt`; only 1,188
orders carry any tax. This is a wholesale/B2B pattern.

**Recommendation:** carry order-level tax as recorded; do **not** infer a
customer-level tax exemption in Shopify from an order flag. That is a tax
position, not a data mapping, and needs the owner's or their advisor's sign-off.

---

## Decisions blocked on Shopify access

| # | Item | Gate |
|---|---|---|
| T-2 | `priceSet` per-unit vs line-total semantics | **REQUIRES SHOPIFY ACCESS/SCOPE** |
| T-3 | Historical transaction/gateway representation | **REQUIRES SHOPIFY ACCESS/SCOPE** |
| T-5 | Tax allocation: order-level vs per-line | **REQUIRES SHOPIFY ACCESS/SCOPE** |
| T-8 | Throttle cost per `orderCreate` → runtime estimate | **REQUIRES SHOPIFY ACCESS/SCOPE** |

None can be answered by assumption. All require Gate O-1 (`read_orders` /
`write_orders`), which is unmet.

---

## Sign-off

No decision above is approved. Recommendations do not become decisions by
silence, by a technical PASS, or by "looks good". Each requires an explicit
statement naming the decision and the option chosen.

---

# Resolution log — Gate O-3 working session

Investigations run after the register was first written. Each converts a
previously UNKNOWN item into measured evidence. **Shopify mutations: 0.**

## O-4 / D-3 — RESOLVED: the 247 exclusion was deliberate

`migration/schema/phase10_migration_contract.json` (frozen 2026-08-22) documents
it explicitly — **VERIFIED**:

```
policy_view.deferred_name_conflicts : 247
policy_view._deferred_note : "Held, not dropped. Inside the 12,096. They import
                              unchanged once a reviewer confirms their names
                              (ADR-014 Gate 5)."
run_population._derivation : "12096 IMPORT - 247 deferred = 11849"
architecture.identity_merging : "NEVER automatic. The 247 conflicting-name
                              customers must not be merged or imported under an
                              assumed identity."
```

This is **not** a Phase 10 completeness gap. The 247 are name-conflict records
deliberately held pending reviewer confirmation. Consequently the 104 Phase 11
orders that reference them are blocked on **ADR-014 Gate 5**, not on any defect.

Gate O-4 is therefore **CLEARED** as a technical question. The remaining Phase 11
choice is only how to treat those 104 orders while Gate 5 is outstanding.

## D-10 — RESOLVED: only two non-ISO country values exist

Full targeted pass over `wp_wc_order_addresses` — **VERIFIED**:

| Value | Address rows | Shape |
|---|---:|---|
| `United Kingdom` | 78 | non-ISO |
| `Reino Unido` | 2 | non-ISO (Spanish for United Kingdom) |

16 distinct country values in total; 14 are valid ISO codes (GB 16,046 · FR 71 ·
IE 67 · DE 44 · US 36 · NL 22 · IT 20 · ES 9 · BE 8 · NO 4 · CA 3 · NG 2 …).
**Only 40 orders are affected**, and both values map unambiguously to `GB`.

This is now a trivial, low-risk mapping rather than an open unknown. No country
is guessed: both strings denote the United Kingdom explicitly.

## D-5 — RESOLVED: 12 orphan line items across 3 products

**VERIFIED.** The 12 line items where the parent product is live but the
variation is absent from the Phase 9 variant map:

| Parent product | Variation | Line items | Note |
|---|---:|---:|---|
| 18 — Vee Beauty Total Coverage Foundation | 3992 | 6 | **Not** the known broken variation (10966) |
| 60 — Nuban Beauty In My Skin Concealer | 523 | 3 | No skipped-variation record |
| 1720 — Eos Body Lotion Vanilla Cashmere | 14348 | 3 | No skipped-variation record |

None corresponds to a Phase 9 quarantined variation. The likely explanation is
that these variations existed when the orders were placed and were deleted from
WooCommerce before the export — **INFERRED**, consistent with the 11 deleted
parent products found under D-4.

Treatment follows D-4: supply `productId` (all three parents are live), omit
`variantId`, carry `variantTitle` as text.

## Phase 10 live verification — independent of the ledger

Paged the live store: **11,844 customers, all 11,844 carrying
`custom.legacy_woo_customer_id`** — an exact match to the ledger's distinct GID
count. **VERIFIED.**

Note for anyone re-checking this: `customersCount` returns
`{count: 10000, precision: AT_LEAST}` on this store. **10,000 is a floor, not the
customer count.** Reading it as an exact figure would understate the population
by 1,844.

## Governance item raised for the owner — Phase 10 Gate 7

Stated factually, for human resolution; this is not a Phase 11 technical blocker.

| Source | Says |
|---|---|
| `phase10_migration_contract.json` → `authorization.granted` | **`False`** |
| `phase10_migration_contract.json` → `authorization._note` | "No paraphrase, no previous approval, and no engineering recommendation is a substitute." |
| `docs/PHASE10_CURRENT_STATE.md` | "ADR-014 Gate 7 — **requested, unsigned**" |
| `docs/PHASE10_DECISION_MATRIX.md` | "Gate 7 is the only thing that authorizes a run" |
| `phase10_bulk_import_executor_plan.json` | "ADR-014 Gate 7 is UNSIGNED. This is a plan, not permission." |
| `phase10_bulk_import_executor_result.json` | `mode: "execute"`, 16,492 mutations |
| **Live store** | **11,844 customers exist** |

The bulk customer import executed, and its artifacts still record the
authorizing gate as ungranted. The most likely explanation is that Gate 7 was
signed and the artifacts were never updated to reflect it.

**Why it matters for Phase 11:** order import is larger and, unlike customers,
**not reversible** — Shopify orders cannot be deleted. Before Phase 11 writes
anything under the same governance model, the owner should confirm whether Gate 7
was in fact signed, and if so update `authorization.granted` so the contract
matches reality. If it was not signed, that should be established before an
irreversible phase begins.

---

# DECISIONS RECORDED — 2026-08-24, project/store owner

Answered in the Gate O-3 working session. Scope computed by
`migration/scripts/phase11_approved_scope.py` → `reports/phase11_approved_scope.{json,csv}`.

| Decision | Chosen | Effect |
|---|---|---|
| **D-1** | Migrate all statuses **except `wc-checkout-draft`** | 2 orders excluded (£43.00) |
| **D-2** | Guest orders: **email only, no customer association** | 6,735 orders; no identity inferred |
| **D-4** | Absent products: **`productId: null`**, retain title/sku/price | 285 line items import unlinked |
| **D-5** | Orphan variations: supply `productId`, omit `variantId` | 77 line items |
| **Gate 7** | Owner confirms it **was signed**; artifacts corrected retrospectively | `phase10_migration_contract.json` updated with provenance |

## Resulting import cohort

| Disposition | Orders | Value |
|---|---:|---:|
| **APPROVED_FOR_IMPORT** | **8,053** | **£464,097.26** |
| PENDING_DECISION (open items) | 154 | £9,910.23 |
| EXCLUDED_D1_checkout_draft | 2 | £43.00 |
| **Total** | **8,209** | **£474,050.49** |

**97.9% of orders and 97.9% of value are approved for import.**

## A `MISSING_ORDER_DATA` exception D-1 removed for free — VERIFIED by ID

The 2 orders with no line items are order IDs **`[15690, 15698]`**, and the 2
`wc-checkout-draft` orders are order IDs **`[15690, 15698]`** — *identical sets*,
confirmed by ID rather than inferred from matching counts. Excluding
checkout-drafts therefore eliminates the `MISSING_ORDER_DATA` class entirely.

## Still open — held, never silently resolved

These 154 orders are tagged `PENDING_DECISION` in `phase11_approved_scope.csv`
with the specific hold recorded per order. Counts sum exactly to 154, so no order
carries more than one hold.

| Hold | Orders | Note |
|---|---:|---|
| `D-3_unmapped_customer` | 126 | 104 blocked on ADR-014 Gate 5 (the 247 deferred name-conflicts), 21 Phase 10 quarantined, 1 failed |
| `D-7_source_financial_variance` | 20 | Was 22; 2 were checkout-drafts, now excluded by D-1 |
| `D-6_non_gbp` | 4 | EUR 3, USD 1 |
| `D-9_refund_complex` | 4 | Multiple refunds per order |

D-6, D-7 and D-9 have standing recommendations (manual review; import with
declared variance, never adjust; manual review). D-3 is the material one — 126
orders, £8,449.97 — and reduces to a single question now that Gate O-4 is closed:
treat unmapped-customer orders as email-only like guests, or hold them until
ADR-014 Gate 5 releases the 247.
