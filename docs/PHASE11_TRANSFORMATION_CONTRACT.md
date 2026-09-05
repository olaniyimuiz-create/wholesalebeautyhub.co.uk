# Phase 11 — Order Transformation Contract

**Architecture only. No executor exists. Shopify mutations performed: 0.**

Defines the field-by-field mapping from WooCommerce HPOS to Shopify
`orderCreate` for the 8,209 historical orders. Every Shopify input field named
here was confirmed to exist by **live schema introspection against API version
2026-07** during this session — none is assumed from documentation or memory.

Labels: **VERIFIED** · **INFERRED** · **UNKNOWN** · **REQUIRES BUSINESS
DECISION** · **REQUIRES SHOPIFY ACCESS/SCOPE**

Nothing in this contract is authorized for execution. Gate O-1 (`read_orders` /
`write_orders`) is unmet, so no part of it has been exercised against Shopify.

---

## 1. Capability baseline — what was actually confirmed

**VERIFIED — `OrderCreateOrderInput` accepts 32 fields**, including the four
that make historical import viable at all:

| Field | Type | Why it matters |
|---|---|---|
| `processedAt` | `DateTime` | Back-dates the order. Shopify's supported historical mechanism |
| `transactions` | LIST | Records historical payment without re-charging a gateway |
| `taxLines` | LIST | Carries source tax rather than recomputing it |
| `metafields` | LIST | Carries `custom.legacy_woo_order_id` idempotency key |

Also present: `billingAddress`, `shippingAddress`, `shippingLines`, `lineItems`,
`customer`, `email`, `phone`, `currency`, `presentmentCurrency`,
`financialStatus`, `fulfillmentStatus`, `fulfillment`, `discountCode`, `note`,
`tags`, `customAttributes`, `name`, `poNumber`, `closedAt`, `taxesIncluded`,
`sourceName`, `sourceIdentifier`, `sourceUrl`, `referringSite`,
`buyerAcceptsMarketing`, `test`, `userId`, `companyLocationId`.

**VERIFIED — `OrderCreateOptionsInput` has exactly three fields**, and all three
are safety-critical:

| Option | Values | Required setting | Rationale |
|---|---|---|---|
| `inventoryBehaviour` | `BYPASS` · `DECREMENT_IGNORING_POLICY` · `DECREMENT_OBEYING_POLICY` | **`BYPASS`** | Documented as "Do not claim inventory." Historical orders must not decrement the live stock Phase 9 set |
| `sendReceipt` | Boolean | **`false`** | Suppresses order confirmation email |
| `sendFulfillmentReceipt` | Boolean | **`false`** | Suppresses shipping confirmation email |

The `BYPASS` enum member is now pinned by name — previously UNKNOWN.

**VERIFIED — `RefundInput` supports back-dated refunds.** It exposes
`processedAt: DateTime` ("The date and time when the refund is being processed")
and `notify: Boolean`. This retires the ADR's largest fidelity risk (R-5): refund
dates *can* be preserved. It also exposes `refundLineItems`, `shipping`,
`transactions`, `refundMethods`, `allowOverRefunding` and `discrepancyReason`.

**VERIFIED — `OrderCreateCustomerInput` offers two mutually exclusive modes:**
`toAssociate` (link an existing customer by ID) and `toUpsert` (create or update
a customer). **`toUpsert` must never be used.** It would create customers outside
the Phase 10 ledger, breaking that phase's idempotency guarantee and its audit
trail. This is a hard build constraint.

---

## 2. Order header mapping

| Woo source | Shopify target | Status |
|---|---|---|
| `wp_wc_orders.id` | `metafields[custom.legacy_woo_order_id]` | **INFERRED** — mirrors the proven `custom.legacy_woo_id` / `custom.legacy_woo_customer_id` pattern |
| `date_created_gmt` | `processedAt` | **VERIFIED** capability |
| `date_paid_gmt` | `metafields[custom.woo_date_paid]` | **INFERRED** |
| `date_completed_gmt` | `metafields[custom.woo_date_completed]` | **INFERRED** |
| `date_updated_gmt` | `metafields[custom.woo_date_updated]` | **INFERRED** |
| `currency` | `currency` + `presentmentCurrency` | **VERIFIED** |
| `total_amount` | reconciliation target only — never sent | **INFERRED**; Shopify derives the total from components |
| `tax_amount` | `taxLines[].priceSet` | **VERIFIED** field; allocation method **UNKNOWN** (see §5) |
| `customer_note` | `note` | **INFERRED** |
| `payment_method_title` | `transactions[].gateway` or a tag | **UNKNOWN** — representation undecided |
| `transaction_id` | `metafields[custom.woo_transaction_id]` | **INFERRED** |
| `ip_address`, `user_agent` | **not migrated** | **INFERRED** — personal data with no merchandising value |
| `status` | `financialStatus` + `fulfillmentStatus` | **REQUIRES BUSINESS DECISION** (§4) |
| — | `sourceName` = `"woocommerce-migration"` | **INFERRED** — marks provenance |
| — | `tags` += `migrated-from-woocommerce`, `woo-<status>` | **INFERRED** |

**Shopify's `createdAt` is its own ingestion timestamp and will differ from the
WooCommerce date. No artifact may present it as the historical date.** Only
`processedAt` and the `custom.woo_date_*` metafields carry historical truth.

---

## 3. Line item mapping

| Woo itemmeta | Shopify `OrderCreateLineItemInput` | Status |
|---|---|---|
| `_product_id` | `productId` (Phase 9 map → GID) | **VERIFIED** field |
| `_variation_id` | `variantId` (Phase 9 variant map → GID) | **VERIFIED** field |
| `_qty` | `quantity` | **VERIFIED** |
| `_line_subtotal` | `priceSet` — pre-discount unit basis | **VERIFIED** field; per-unit vs line-total semantics **UNKNOWN** |
| `_line_tax`, `_line_subtotal_tax` | `taxLines[]` on the line item | **VERIFIED** field |
| item name | `title` | **VERIFIED** |
| — | `sku`, `variantTitle`, `vendor`, `taxable`, `requiresShipping` | **VERIFIED** fields, optional |

**VERIFIED and important:** `productId` is documented as nullable — "Can be
`null` if the original product associated with the order is deleted." Combined
with `title`, `sku` and `priceSet`, this means **orders referencing deleted
products can still be imported faithfully as unlinked line items**, preserving
financial history without fabricating a product. This directly serves the 262
`MISSING_PRODUCT` orders (£24,164.65) — see the decision register.

---

## 4. Status mapping — **REQUIRES BUSINESS DECISION**

**VERIFIED enums:** financial `PENDING, AUTHORIZED, PARTIALLY_PAID, PAID,
PARTIALLY_REFUNDED, REFUNDED, VOIDED, EXPIRED`; fulfillment `FULFILLED, PARTIAL,
RESTOCKED`. Note there is **no `CANCELLED` fulfillment value** and no single
"order status" field.

| Woo status | Orders | Proposed financial | Proposed fulfillment | Label |
|---|---:|---|---|---|
| `wc-completed` | 7,595 | `PAID` | `FULFILLED` | **INFERRED** |
| `wc-failed` | 423 | `VOIDED` or `EXPIRED` | none | **REQUIRES BUSINESS DECISION** |
| `wc-cancelled` | 122 | `VOIDED` | none | **INFERRED** |
| `wc-refunded` | 54 | `REFUNDED` | `FULFILLED` | **INFERRED** |
| `wc-pending` | 7 | `PENDING` | none | **INFERRED** |
| `wc-processing` | 6 | `PAID` | none | **INFERRED** |
| `wc-checkout-draft` | 2 | — | — | **REQUIRES BUSINESS DECISION** — abandoned carts, recommend exclude |

Whether the 554 non-completed orders (£31,309.29) are migrated at all is
decision **D-1**.

---

## 5. Financial contract

**Source model — VERIFIED.** WooCommerce records tax at three levels at once:
order (`wp_wc_orders.tax_amount`), shipping and discount
(`wp_wc_order_operational_data.shipping_tax_amount` / `discount_tax_amount`),
and line (`_line_tax`, `_line_subtotal_tax`). `prices_include_tax` is per order.

**Materiality — VERIFIED.** Only 1,188 of 8,209 orders carry any tax, and 8,168
are flagged `is_vat_exempt` — a wholesale/B2B pattern. Discounts total **£35.20
across the entire history**; fee lines number **zero**.

**Arithmetic rules — INFERRED design:**

1. `Decimal` throughout. **Float is prohibited** anywhere in the money path.
2. Source values are transported verbatim; no recomputation, no normalization, no FX conversion.
3. Quantize to 2dp only at comparison and output.
4. Tolerance **±0.01** per component and on the order total.
5. Shopify derives the order total from components — the source `total_amount` is a **reconciliation target, never an input**.

**Known pre-existing variance — VERIFIED.** 22 orders already fail their own
internal arithmetic in WooCommerce (subtotal − discount + shipping + tax + fees
≠ stored total), by −0.71 to +0.29; e.g. order 2289 recomputes 13.09 against a
stored 13.00. This is a **source defect, not a migration defect**. It must be
carried as declared variance, never silently corrected — decision **D-7**.

---

## 6. Refund contract — **VERIFIED** capability

146 refund records: 52 full, 86 partial, 4 multiple, totalling £4,827.15.

`refundCreate` accepts `orderId`, `processedAt` (back-dating), `notify`
(**must be `false`**), `refundLineItems`, `shipping`, `transactions` and
`discrepancyReason`. A refund is a **second mutation after its parent order
exists** — so the executor must be two-pass, and the parent's Shopify GID must be
in the ledger before its refund is attempted.

The 4 multiple-refund orders remain `REFUND_COMPLEX` → **MANUAL_REVIEW** until a
human confirms each sequence reconciles.

---

## 7. Address contract

8,209 orders have a billing row; 8,205 have shipping. `MailingAddressInput`
carries the standard fields.

**VERIFIED defect:** distinct billing `country` values include `Re` and `Un`.
A valid 2-character ISO code truncated to 2 characters is unchanged, so these
prove **longer, non-ISO country strings are stored** (e.g. "Republic of…",
"United…"). Shopify requires ISO codes. Full value set and affected order count
are **UNKNOWN** — one targeted pass is needed before any import. Unmappable
values must go to `MANUAL_REVIEW`, never be guessed.

---

## 8. Customer association

| Case | Orders | Treatment | Status |
|---|---:|---|---|
| Registered, mapped | 1,348 | `customer.toAssociate` with the Phase 10 GID | **INFERRED** |
| Guest (`customer_id = 0`) | 6,735 | `email` only, no customer association — or attach by email | **REQUIRES BUSINESS DECISION** (D-2) |
| Registered, unmapped | 126 | See root-cause analysis | **REQUIRES BUSINESS DECISION** (D-3) |

`toUpsert` is prohibited in all three cases.

---

## 9. Idempotency, resume, audit

**Key:** `custom.legacy_woo_order_id`. **Lookup: full scan only.** Shopify's
metafield search is proven unreliable — `legacy_woo_id:18` returned the wrong
product during Phase 9 — so search must never be the identity mechanism
(**VERIFIED** constraint).

**Ledger:** append-only JSONL — Woo order ID → Shopify Order GID → timestamp →
batch → action → `userErrors` → reconciliation status. Same shape as
`phase10_bulk_import_ledger.jsonl`. No PII.

**Resume:** from the checkpoint, never inferred from Shopify state alone.

---

## 10. Reversibility — the reason this phase carries more risk

**VERIFIED constraint:** Shopify orders cannot be deleted. They can only be
cancelled or closed. Unlike products (deletable) and customers (deletable), a
mis-imported order is **permanent**. Rollback is therefore *remediation*, not
*reversal*, and this asymmetry should weigh on Gate O-6 more than it did on the
equivalent product or customer gate.

---

## 11. Open technical items

| # | Item | Status |
|---|---|---|
| T-1 | `read_orders` / `write_orders` not granted | **REQUIRES SHOPIFY ACCESS/SCOPE** |
| T-2 | `priceSet` semantics — per-unit or line total | **UNKNOWN** |
| T-3 | Historical transaction/gateway representation | **UNKNOWN** |
| T-4 | Non-ISO country values: count and full set | **UNKNOWN** |
| T-5 | Tax allocation: order-level vs per-line `taxLines` | **UNKNOWN** |
| T-6 | Whether `wp_fct_orders` / `wp_ebay_orders` hold in-scope orders | **UNKNOWN** |
| T-7 | `wp_wc_order_stats` 11,935 rows vs 8,209 orders | **UNKNOWN** |
| T-8 | Throttle cost per `orderCreate` → runtime | **REQUIRES SHOPIFY ACCESS/SCOPE** |

No item above may be resolved by assumption. T-2, T-3 and T-5 are answerable
only by exercising the API, which Gate O-1 currently forbids.
