# Phase 11 — Historical Orders Migration (ADR)

**Status: BLOCKED — analysis complete, execution not authorized.**
**Shopify mutations performed producing this document: 0.**

Every figure below was computed this session by
`migration/scripts/phase11_order_inventory.py` from `migration/sql/dump.sql`
(854,674 order-table rows) and by live Shopify schema introspection against API
version **2026-07**. No figure is inherited from prior documentation.

Evidence labels: **VERIFIED** (measured from source or live API this session) ·
**INFERRED** (reasoned from verified evidence, not directly measured) ·
**UNKNOWN** (not determinable from available evidence).

---

## 1. Objective

Represent 8,209 historical WooCommerce orders in the Shopify development store
with faithful financial values, correct customer/product/variant association,
and an audit trail that never claims Shopify's native timestamps are the
historical WooCommerce dates. **VERIFIED** as scope; execution unauthorized.

## 2. Source-of-truth hierarchy

**VERIFIED.** This store runs WooCommerce **HPOS**. Authority, highest first:

1. `wp_wc_orders` — order header, status, currency, totals, customer, payment
2. `wp_wc_order_operational_data` — paid/completed dates, shipping & discount totals and their tax
3. `wp_wc_order_addresses` — billing/shipping addresses
4. `wp_woocommerce_order_items` + `wp_woocommerce_order_itemmeta` — line items (still legacy tables)
5. `wp_wc_orders_meta` — payment gateway metadata

`wp_wc_order_stats`, `wp_wc_order_product_lookup`, `wp_wc_order_tax_lookup`,
`wp_wc_order_coupon_lookup` are **analytics projections, cross-check only**.
`wp_wc_order_stats` holds **11,935 rows against 8,209 orders** — it must never
be used as an order count. **VERIFIED.**

Non-WooCommerce order tables exist and are **out of scope**: `wp_fct_orders`
(FluentCart), `wp_ebay_orders`, `wp_wdp_orders`, `wp_wdr_order_discounts`.
Whether any carries orders absent from `wp_wc_orders` is **UNKNOWN**.

## 3. Dependency hierarchy

**VERIFIED.** Woo order → Woo customer ID → Shopify Customer GID (Phase 10
ledger). Woo line item → Woo product/variation ID → Shopify Product/Variant GID
(Phase 9 manifest + variant mapping). Phase 11 consumes these maps and creates
no products or customers.

## 4. Order population — **VERIFIED**

| Metric | Value |
|---|---:|
| `wp_wc_orders` rows (all types) | 8,355 |
| **`shop_order`** | **8,209** |
| `shop_order_refund` | 146 |
| Other order types | none |
| Date range (GMT) | 2024-06-06 14:54:46 → 2025-11-10 21:57:06 |
| Line items | 20,544 |
| Total quantity | 22,729 |
| Order-item rows (all types) | 46,594 |
| Orders with no line items | 2 |

**By status:** `wc-completed` 7,595 · `wc-failed` 423 · `wc-cancelled` 122 ·
`wc-refunded` 54 · `wc-pending` 7 · `wc-processing` 6 · `wc-checkout-draft` 2

**By year:** 2024 → 2,524 · 2025 → 5,685
**By currency:** GBP 8,205 · EUR 3 · USD 1
**Customer type:** registered 1,474 · guest 6,735
**Composition:** variation lines 987 · simple-only 7,220 · with tax 1,188 ·
with shipping line 8,202 · with coupon 27 · **with fee lines 0**

## 5. Customer mapping — **VERIFIED**

Source: `reports/phase10_bulk_import_ledger.jsonl` → **11,844 mapped WooCommerce
customer IDs** (11,832 `CREATED` + 12 `SKIPPED_ALREADY_PRESENT`).

- 6,735 orders are guest (`customer_id = 0`) — no mapping needed; attach by email or leave unlinked (decision open, §27)
- 1,348 orders map cleanly
- **126 orders reference a customer ID with no Shopify GID** — £8,449.97

**Discrepancy recorded, not resolved:** the Phase 11 brief states "11,835
net-new created, 2 malformed-email quarantined." The ledger says **11,832
created and 5 `FAILED` (`USER_ERROR: Email is invalid`)**. 11,832 + 12 + 5 =
11,849. The brief's 11,835/2 split does not reconcile with the ledger; the
ledger is authoritative. **VERIFIED.**

## 6. Product mapping — **VERIFIED**

Source: `phase9_final_import_manifest.csv`, `ALREADY_IMPORTED` → **596 products**.
**262 orders (£24,164.65)** contain at least one line item whose product ID is
not live in Shopify. Root cause per order is **UNKNOWN** — candidates are the 15
Phase 9 quarantined products and products deleted from WooCommerce before export.

## 7. Variant mapping — **VERIFIED**

Source: `phase9_variant_mapping.csv` → **497 variations**. **58 orders
(£5,480.19)** reference a variation ID with no mapping. Combined product+variant
exposure: **372 line items, £11,562.87**.

## 8. Status mapping — proposed, **INFERRED**

Shopify has no single "order status"; it has `financialStatus` and
`fulfillmentStatus`. Introspection confirms the accepted enums (**VERIFIED**):
financial `PENDING, AUTHORIZED, PARTIALLY_PAID, PAID, PARTIALLY_REFUNDED,
REFUNDED, VOIDED, EXPIRED`; fulfillment `FULFILLED, PARTIAL, RESTOCKED`.

| Woo status | n | Financial | Fulfillment | Basis |
|---|---:|---|---|---|
| `wc-completed` | 7,595 | `PAID` | `FULFILLED` | INFERRED |
| `wc-failed` | 423 | `VOIDED` or `EXPIRED` | none | **UNKNOWN — open decision** |
| `wc-cancelled` | 122 | `VOIDED` | `RESTOCKED`? | INFERRED |
| `wc-refunded` | 54 | `REFUNDED` | `FULFILLED` | INFERRED |
| `wc-pending` | 7 | `PENDING` | none | INFERRED |
| `wc-processing` | 6 | `PAID` | none | INFERRED |
| `wc-checkout-draft` | 2 | — | — | **UNKNOWN — likely exclude, abandoned carts** |

Unresolved: whether failed/cancelled/draft orders (554 orders, £31,309) should
be migrated at all. Importing them creates Shopify orders that never represent
real revenue; excluding them loses history. **Business decision, §27.**

## 9. Financial mapping — **VERIFIED** totals

Pooled across all currencies (source values preserved, no normalization):

| Component | Amount |
|---|---:|
| Line subtotal | 427,220.51 |
| Discounts | 35.20 |
| Shipping | 37,349.37 |
| Tax | 9,483.00 |
| Fees | 0.00 |
| Refunds | 4,827.15 |
| **Order total** | **474,050.49** |

By currency: **GBP 473,727.39** · EUR 307.10 · USD 16.00.
By status, `wc-completed` alone is **438,955.88**.

## 10. Tax mapping — **VERIFIED** structure

WooCommerce stores tax at multiple levels simultaneously: order-level
(`wp_wc_orders.tax_amount`), shipping tax and discount tax
(`wp_wc_order_operational_data`), and line level (`_line_tax`,
`_line_subtotal_tax` itemmeta). `prices_include_tax` is recorded per order.

Only **1,188 of 8,209 orders carry tax**, and **8,168 orders are flagged
`is_vat_exempt`** — consistent with a wholesale/B2B customer base. Shopify's
`taxLines` and `taxesIncluded` can represent this. Whether to import VAT-exempt
status as a customer tax exemption is **UNKNOWN — open decision**.

## 11. Shipping mapping — **VERIFIED**

8,202 orders carry a shipping line; total 37,349.37. Shopify `shippingLines`
accepts title and price. Woo `method_id`/`method_title` → Shopify shipping line
title is **INFERRED** as a direct copy.

## 12. Discount mapping — **VERIFIED**, low materiality

Only **27 orders** carry a coupon line and total discounts are **£35.20** across
the entire history. `OrderCreateOrderInput` exposes a single
`discountCode` (`OrderCreateDiscountCodeInput`); multi-coupon orders may not be
representable. Given the £35.20 exposure, recording discounts as order-level
value plus a note is a defensible simplification — **decision open**.

## 13. Refund mapping — **VERIFIED** counts

146 refund records against 8,209 orders: **no refund 8,067 · full 52 · partial
86 · multiple 4**. Total refunded 4,827.15.

`refundCreate` exists on 2026-07 (**VERIFIED**). Whether a refund can be
back-dated to its original WooCommerce date is **UNKNOWN** and is the single
biggest fidelity risk in this phase. The 4 multiple-refund orders are tagged
`REFUND_COMPLEX` → **MANUAL_REVIEW**.

## 14. Transaction / payment mapping — **VERIFIED** source

Gateway is WooPayments/Stripe: `_wcpay_transaction_fee` (14,901 rows),
`_stripe_customer_id` (7,859), `_charge_id` (7,718), `_intent_id` (7,685).
8,200 orders have a payment method; 7,688 have a transaction ID; **9 orders have
neither** → `PAYMENT_AMBIGUOUS`.

`OrderCreateOrderInput.transactions` exists (**VERIFIED**). Historical
transactions cannot be re-executed against a real gateway; they must be recorded
as informational. Exact representation is **UNKNOWN** pending Gate O-2 research.

## 15. Historical date strategy — **VERIFIED** capability

`OrderCreateOrderInput.processedAt: DateTime` exists on 2026-07 — Shopify's
supported mechanism for back-dating an imported order. `closedAt` also exists.

`createdAt` remains Shopify's own ingestion time and **must never be presented
as the WooCommerce date**. Proposed: set `processedAt` from
`date_created_gmt`, and preserve `date_paid_gmt`, `date_completed_gmt`,
`date_updated_gmt` in metafields under `custom.*`, plus
`custom.legacy_woo_order_id` as the idempotency key. **INFERRED design, not implemented.**

## 16. Inventory strategy — **VERIFIED** capability

`OrderCreateOptionsInput.inventoryBehaviour` exists, documented as "not claiming
inventory, ignoring inventory policies, or following policies." Historical
orders must **not** decrement live stock — Phase 9 set real quantities. Proposed
value: the non-claiming option. Exact enum member to be pinned at build time.

## 17. Notification strategy — **VERIFIED** capability

`OrderCreateOptionsInput.sendReceipt` and `sendFulfillmentReceipt` both exist and
default to not sending unless set. Both **must be explicitly false** — emailing
11,849 migrated customers about two-year-old orders would be a serious incident.
This is a hard build requirement.

## 18. Currency strategy — **VERIFIED**

8,205 GBP, 3 EUR, 1 USD. Store currency is GBP. `currency` and
`presentmentCurrency` exist on the input. The 4 non-GBP orders (£323.10) are
tagged `UNSUPPORTED_CURRENCY` → **MANUAL_REVIEW** rather than silently converted.
**No FX conversion will be invented.**

## 19. Exception strategy — **VERIFIED** counts

Tags are not mutually exclusive; `IMPORT_READY` is assigned only when no other
tag applies.

| Class | Orders | Value |
|---|---:|---:|
| **IMPORT_READY** | **7,780** | 439,842.59 |
| MISSING_PRODUCT | 262 | 24,164.65 |
| MISSING_CUSTOMER | 126 | 8,449.97 |
| MISSING_VARIANT | 58 | 5,480.19 |
| FINANCIAL_MISMATCH | 22 | 1,085.00 |
| PAYMENT_AMBIGUOUS | 9 | 194.03 |
| REFUND_COMPLEX | 4 | 95.16 |
| UNSUPPORTED_CURRENCY | 4 | 323.10 |
| MISSING_ORDER_DATA | 2 | 43.00 |

**94.8% of orders are clean.**

## 20. Reconciliation strategy — **INFERRED design**

An order is financially PASS only when the transformed representation reconciles
to source; "order created successfully" is never financial validation.

Per order, compare independently: line subtotal, discounts, shipping, tax, fees,
refunds, final total. Rounding: `Decimal` throughout, never float; quantize to
2dp at comparison only; tolerance **±0.01** per component and on the total.

Applying that rule to source already reveals **22 orders whose recomputed total
disagrees with the stored total** by −0.71…+0.29 (e.g. order 2289 recomputes
13.09 vs stored 13.00). This is a **pre-existing WooCommerce inconsistency, not
a migration defect**, and must be carried as a known variance rather than
silently corrected. **VERIFIED.**

## 21. Idempotency strategy — **INFERRED**

`custom.legacy_woo_order_id` metafield, mirroring `custom.legacy_woo_id`
(products) and `custom.legacy_woo_customer_id` (customers). Query-before-write
via **full scan**, never metafield search — Phase 9 proved Shopify's metafield
search returns wrong products (`legacy_woo_id:18` returned the wrong product).
**VERIFIED constraint.**

## 22. Resume strategy — **INFERRED**

Append-only checkpoint keyed on Woo order ID, matching
`phase10_bulk_import_checkpoint.jsonl`. Resume re-reads the checkpoint and skips
completed IDs; never re-derives progress from Shopify state alone.

## 23. Audit strategy — **INFERRED**

Per-order ledger: Woo order ID → Shopify Order GID → timestamp → batch → action
→ userErrors → reconciliation status. Same shape as
`phase10_bulk_import_ledger.jsonl`. No customer PII in any artifact.

## 24. Rollback strategy — **INFERRED**

No mass deletion. Shopify orders cannot be truly deleted, only cancelled/closed
— which makes order import **substantially less reversible than products or
customers**. This raises the bar for Gate O-5. Remediation is targeted and
human-reviewed via the ledger's GID record.

## 25. Tier-3 test strategy — **INFERRED**

Mirroring Phase 10: a small frozen cohort covering completed, refunded (full and
partial), guest, registered, variation-bearing, tax-bearing, shipping-bearing and
multi-line orders; execute; reconcile field-by-field against fresh queries;
re-run to prove idempotency; human review before any bulk authorization.

## 26. Bulk execution strategy — **INFERRED**

Controlled batches on the Phase 9/10 precedent. Cost is **UNKNOWN** until Gate
O-1 clears: `orderCreate` is at minimum 1 mutation/order (8,209), plus refunds
(146) and verification reads. Runtime cannot be responsibly estimated until
throttle cost per `orderCreate` is measured live. **No runtime is asserted here.**

## 27. Open business decisions

1. **Migrate non-completed orders?** 554 orders / £31,309 across failed, cancelled, pending, processing, checkout-draft.
2. **Guest orders (6,735, 82%)** — attach to a Shopify customer by email, or import unlinked?
3. **Refund fidelity** — accept refunds recorded at import date rather than original date, if back-dating proves impossible?
4. **Non-GBP orders (4)** — manual review, exclude, or record at source value?
5. **VAT-exempt flag (8,168 orders)** — carry forward as customer tax exemption?
6. **Discounts** — accept order-level simplification given the £35.20 total exposure?
7. **The 22 pre-existing financial variances** — import as-is with a note, or quarantine?

## 28. Open technical decisions

1. `read_orders` / `write_orders` scope grant — **hard blocker, see §30**
2. Exact `inventoryBehaviour` enum member
3. Whether `refundCreate` accepts a historical date
4. Historical transaction representation without a live gateway
5. Non-ISO billing country values — **VERIFIED that they exist**: distinct billing countries include `Re` and `Un`, which cannot be truncations of valid 2-character ISO codes and therefore prove longer, non-ISO values are stored. Shopify requires ISO country codes. Count and full value set **UNKNOWN**.
6. Whether `wp_fct_orders` / `wp_ebay_orders` contain in-scope orders
7. Reconciling `wp_wc_order_stats` (11,935 rows) against 8,209 orders

## 29. Risks

| # | Risk | Severity |
|---|---|---|
| R-1 | Order creation is not cleanly reversible — no true delete | **HIGH** |
| R-2 | Customer notification emails to 11,849 people if `sendReceipt` is not suppressed | **HIGH** |
| R-3 | Live inventory decremented by historical orders if `inventoryBehaviour` is wrong | **HIGH** |
| R-4 | 262 orders reference products absent from Shopify | MEDIUM |
| R-5 | Refund date fidelity may be impossible | MEDIUM |
| R-6 | 126 orders reference unmapped customers | MEDIUM |
| R-7 | 22 orders carry pre-existing source financial variance | LOW |
| R-8 | Non-ISO country values will be rejected by Shopify | MEDIUM |

## 30. Approval gates

| Gate | Requirement | Type | Status |
|---|---|---|---|
| **O-1** | `read_orders` + `write_orders` granted | TECHNICAL | **BLOCKED — verified live: `ordersCount` → ACCESS_DENIED. App has 24 scopes; neither order scope present** |
| **O-2** | Order representation contract approved (status, tax, transaction, date mapping) | APPROVAL | Open |
| **O-3** | Business decisions §27 resolved | BUSINESS | Open — 7 items |
| **O-4** | Tier-3 test cohort authorized | APPROVAL | Open |
| **O-5** | Test reconciliation accepted by a human | APPROVAL | Not reachable |
| **O-6** | Bulk import authorized, separate from O-4 | APPROVAL | Open |

**No gate is cleared. No order has been created, updated, or queried by GID.**
Phase 11 is *ready for human review*, not ready for import.
