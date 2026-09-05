# Phase 11 — Execution Architecture & Gate Definitions

**Design only. No executor exists and none is authorized.
Shopify mutations performed: 0. `read_orders` / `write_orders`: NOT GRANTED.**

Describes how a Phase 11 importer would be built *if and when* every gate below
is cleared. Nothing here is a licence to build it.

---

## 1. Two-pass structure — **INFERRED design**

Orders and refunds cannot be created in one pass: a refund requires its parent
order's Shopify GID.

```
PASS 1  orders   →  orderCreate   →  ledger: woo_order_id → Order GID
PASS 2  refunds  →  refundCreate  →  requires PASS 1 GID from the ledger
```

Pass 2 must never run for an order absent from the Pass 1 ledger. 146 refunds
attach to 8,209 orders, so Pass 2 is small — but it is strictly ordered after
Pass 1 and reconciled separately.

---

## 2. Mandatory safety options on every call — **VERIFIED capability**

Every `orderCreate` carries `OrderCreateOptionsInput`:

```
inventoryBehaviour:     BYPASS      # "Do not claim inventory"
sendReceipt:            false
sendFulfillmentReceipt: false
```

Every `refundCreate` carries `notify: false`.

These are **not defaults to rely on** — they must be set explicitly on every
call, and a build-time test must assert their presence. Getting
`inventoryBehaviour` wrong would silently decrement the live stock Phase 9 set
across 22,729 units; getting the receipt flags wrong would email thousands of
real customers about orders up to two years old. Both are unrecoverable once
sent.

**A pre-flight guard should refuse to run if any of the four flags is missing
from the request payload**, in the same spirit as the Phase 9 importer's
independent re-derivation of its quarantine set.

---

## 3. Idempotency — **INFERRED design, VERIFIED constraint**

- **Key:** `custom.legacy_woo_order_id`
- **Lookup: full scan only.** Shopify's metafield search returned the wrong
  product during Phase 9 (`legacy_woo_id:18` → wrong product). Search must never
  be the identity mechanism. **VERIFIED constraint.**
- Before CREATE: confirm no existing order carries the key
- Re-run of a completed batch must produce zero creates

Because orders **cannot be deleted** (§6), a duplicate order is permanent. The
idempotency check is therefore stricter here than in Phase 9 or 10, not looser.

---

## 4. Batching, resume and throttling — **partly UNKNOWN**

- Append-only checkpoint keyed on Woo order ID, matching `phase10_bulk_import_checkpoint.jsonl`
- Resume reads the checkpoint; never infers progress from Shopify state alone
- Stop-on-unexpected-error; classify before continuing — never auto-retry in bulk
- Batch size: follow the Phase 9/10 tiered precedent (pilot → small → larger), sized after the first live cost measurement

**Runtime is deliberately not estimated.** Minimum call volume is 8,209
`orderCreate` + 146 `refundCreate` + verification reads, but Shopify's
cost-based throttle charges per query cost, which cannot be measured while Gate
O-1 is unmet. **REQUIRES SHOPIFY ACCESS/SCOPE.** Any number quoted before that
measurement would be invented.

---

## 5. Reconciliation contract — **INFERRED design**

An order is financially PASS **only** when its transformed representation
reconciles against source. `orderCreate` returning success is not validation.

**Per order, compared independently against a fresh Shopify query — never the
importer's own report:**

| Component | Source |
|---|---|
| Line subtotal | Σ `_line_subtotal` |
| Discounts | `discount_total_amount` |
| Shipping | `shipping_total_amount` |
| Tax | `tax_amount` |
| Fees | Σ fee lines (zero in this dataset) |
| Refunds | Σ refund order totals |
| Final total | `total_amount` |

Also compared: line count, quantity, SKU, customer association, `processedAt`,
financial status, fulfillment status, currency.

**Arithmetic rules:** `Decimal` only — float prohibited in the money path;
quantize to 2dp at comparison; tolerance **±0.01** per component and on the
total.

Every row resolves to `MATCH` / `MISMATCH` / `UNRESOLVED` / `NOT_APPLICABLE`.
A batch is never rounded to "passed" while a mismatch is unresolved. The 22
known source variances (D-7) are declared up front as expected variance, so they
are distinguishable from migration-introduced error.

---

## 6. Rollback — **VERIFIED constraint, and it is severe**

**Shopify orders cannot be deleted.** They can only be cancelled or closed. This
makes Phase 11 materially less reversible than Phase 9 (products deletable) or
Phase 10 (customers deletable).

Consequences:

- There is no "undo the batch" operation. Rollback is **remediation, not reversal**
- A duplicate or wrong order is permanent and visible in the store's history
- Therefore the pilot batch must be *small*, and its reconciliation reviewed by a human before any escalation

Maintained instead: import manifest (pre-write), created-order ledger
(woo_order_id → GID → timestamp → batch → action), append-only execution log
with `userErrors`, and per-batch reconciliation state. These enable targeted,
human-reviewed correction of a specific order by known GID — never automated
bulk rollback.

---

## 7. Artifacts the executor would produce

| Artifact | Contents | PII |
|---|---|---|
| `phase11_order_manifest.csv` | Per-order intended disposition, pre-write | IDs only |
| `phase11_import_ledger.jsonl` | woo_order_id → Order GID → ts → batch → action | IDs only |
| `phase11_import_log.jsonl` | Every attempt, `userErrors`, retry count | IDs only |
| `phase11_reconciliation.csv` | Field-level comparison per order | IDs only |
| `phase11_exceptions.csv` | Quarantined orders with reason + human action | IDs only |

Per-record customer PII must never enter these. Order addresses stay in Shopify,
not in repository artifacts. `.gitignore` must be verified before generating any
artifact that could carry address data — the Phase 10 precedent.

---

## 8. Gate definitions and clearance criteria

| Gate | Requirement | Type | Cleared when | Status |
|---|---|---|---|---|
| **O-1** | `read_orders` + `write_orders` granted | TECHNICAL | `ordersCount` returns a number instead of `ACCESS_DENIED` | **BLOCKED** — verified live this session |
| **O-2** | Transformation contract approved | APPROVAL | Named sign-off on `PHASE11_TRANSFORMATION_CONTRACT.md` | Open |
| **O-3** | Business decisions D-1…D-11 resolved | BUSINESS | Each decision explicitly answered, option named | Open — 11 items |
| **O-4** | Phase 10 completeness question resolved | TECHNICAL/GOVERNANCE | The 247 never-attempted `IMPORT` customers confirmed as intended scope, or closed | Open |
| **O-5** | Unknowns T-2/T-3/T-5 answered against the live API | TECHNICAL | Measured, not assumed | Blocked by O-1 |
| **O-6** | Pilot cohort authorized | APPROVAL | Explicit authorization naming the cohort | Open |
| **O-7** | Pilot reconciliation accepted by a human | APPROVAL | Human review recorded | Not reachable |
| **O-8** | Bulk import authorized — separate from O-6 | APPROVAL | Explicit statement | Open |

**No gate is cleared.** O-1 and O-4 are prerequisites to everything else; O-3 is
independent of both and can proceed in parallel, since it needs business input
rather than Shopify access.

---

## 9. Recommended sequence once gates open

1. Grant `read_orders` / `write_orders`; re-run read-only preflight
2. Resolve the 247-customer question (O-4)
3. Answer D-1…D-11 (O-3)
4. Measure `orderCreate` throttle cost on a single dry call; publish a real runtime estimate
5. Build the transformation layer with a **dry-run mode that emits the exact payload without sending it** — reviewable before any mutation
6. Pilot cohort (~10 orders spanning completed, refunded, guest, registered, variation, tax, multi-line); reconcile; re-run to prove idempotency
7. Human review → tiered escalation → per-batch reconciliation → stop on any unexpected drift

Step 5 is the safety-critical one: the payload should be reviewable as data
before it is ever transmitted, exactly as the Phase 9 and Phase 10 dry runs were.

---

## 10. Standing prohibitions for this phase

No `orderCreate`. No `refundCreate`. No `orderUpdate`, `orderEditBegin`,
`orderClose`, or transaction mutation. No customer, product, inventory or
collection mutation. No scope request or app reinstall. No notification of any
kind. No workaround for the missing order scopes. No invented API capability,
FX rate, country code, or financial value. No resolution of an UNKNOWN by
assumption.
