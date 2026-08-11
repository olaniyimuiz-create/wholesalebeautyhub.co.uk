# Phase 10 — WooCommerce → Shopify Customer Field Mapping

Companion to `reports/phase10_customer_mapping.csv` (machine-checkable,
same content). Source: `wp_wc_customer_lookup` (primary), `wp_users` /
`wp_usermeta` (`billing_*`, `shipping_*`, `wp_capabilities`),
`wp_wc_order_addresses` (guest billing fallback), `wp_fc_subscribers`
(FluentCRM — real marketing-consent signal, not currently used by any
existing script). Verified by reading the actual table schemas in
`migration/sql/dump.sql`, not assumed from documentation.

## 1. Identity and core fields

| WooCommerce | Shopify | Notes |
|---|---|---|
| `email` | `email` (CustomerInput) | Primary identifier, dedup key, and the recommended idempotency match key (§ `PHASE10_CUSTOMER_STRATEGY.md`) |
| `first_name` (customer_lookup) / `billing_first_name` (usermeta fallback) | `firstName` | Not required by Shopify; 5,430 of 13,043 raw rows have both names empty — imported anyway, not blocked (see dry-run classification rules) |
| `last_name` / `billing_last_name` | `lastName` | Same as above |
| `username` | **NOT MAPPED** | Shopify customer accounts are email-based; no username field exists in `CustomerInput` (verified via schema introspection) |
| `customer_id` (WooCommerce, `wp_wc_customer_lookup` primary key) | `metafield custom.legacy_woo_customer_id` (**proposed, not yet implemented**) | Mirrors the product `custom.legacy_woo_id` pattern — the idempotency key design in the strategy doc depends on this existing |
| `date_registered` | `metafield custom.woo_registered_at` (**proposed**) | Informational only; Shopify sets its own `createdAt` |
| `wp_capabilities` (role) | Exclusion filter only | `STAFF_ROLES = {administrator, shop_manager, editor}` accounts are never imported as customers — verified: 3 such accounts found and excluded in this dataset |

## 2. Contact

| WooCommerce | Shopify | Notes |
|---|---|---|
| `billing_phone` (usermeta) / order billing phone (guest fallback) | `phone` (CustomerInput) | Shopify requires phone numbers to be **unique** across customers if set — a real collision risk not yet checked against live Shopify (0 customers exist yet, so untested) |

## 3. Address

| WooCommerce | Shopify | Notes |
|---|---|---|
| `billing_address_1`/`_2`, `billing_city`, `billing_state`, `billing_postcode`, `billing_country` (usermeta, or `wp_wc_customer_lookup`'s own city/state/postcode/country columns, or guest order-billing fallback) | `addresses: [CustomerAddressInput]` (one entry, set as default) | 7,367 of 12,096 IMPORT-eligible customers (61%) have no billing address at all — Shopify does not require one; imported without an address |
| `shipping_address_1`/`_2`, `shipping_city`, `shipping_state`, `shipping_postcode`, `shipping_country` (usermeta) | **NOT CURRENTLY MAPPED** | Real gap: `database_parser.py`'s `is_customer_meta_key()` already captures these into `usermeta` (its whitelist includes any `shipping_`-prefixed key), but `build_customers()` never reads them out — `migration/data/customers.json` and the pre-built `shopify_customers_import.csv` both carry **billing only**. 1,209 of 12,096 customers (10%) have a real shipping address that is currently discarded, not migrated. Shopify supports a second `CustomerAddressInput` for this — a genuine, fixable gap, not a platform limitation. No decision has been made yet on whether to add it. |
| `Default Address Province Code` (existing CSV) | raw WooCommerce state/county string (e.g. "England", "Wales") | Pre-existing, documented limitation (`docs/ARCHITECTURE.md` § Known limitations) — GB addresses don't have Shopify-recognized province codes; harmless, not normalized |

## 4. Marketing consent

See `docs/PHASE10_GDPR_CONSENT.md` for the full policy discussion — this
row is the schema mapping only, not the decision.

| WooCommerce/plugin | Shopify | Notes |
|---|---|---|
| `wp_fc_subscribers.status` (FluentCRM — real signal for 6,545 of 12,096 customers: 6,295 subscribed / 229 unsubscribed / 21 pending) | `emailMarketingConsent.marketingState` (**proposed, NOT YET APPROVED**) | `marketingState` only accepts `SUBSCRIBED`/`UNSUBSCRIBED`/`PENDING` as input (`NOT_SUBSCRIBED`/`REDACTED`/`INVALID` are read-only, verified via schema introspection) |
| No FluentCRM record at all (5,551 of 12,096) | `emailMarketingConsent` field omitted entirely | Omitting the field leaves Shopify's own default, `NOT_SUBSCRIBED` — unknown consent is never represented as an active subscription state |
| `wp_wc_email_unsubscribes` | N/A | Table exists but is **empty** (0 rows) — checked directly, no additional signal here |
| SMS/WhatsApp consent | **NOT MAPPED** | No SMS or WhatsApp opt-in data exists anywhere in the source tables read by this pipeline |

## 5. Notes and unsupported fields

| WooCommerce | Shopify | Notes |
|---|---|---|
| Order-level `customer_note` | **NOT MAPPED** | WooCommerce's `customer_note` is per-order, not a customer-profile field — no customer-level note exists in the source data this pipeline reads |
| WooCommerce password hash | **NOT MAPPED — technically impossible** | This store uses `NEW_CUSTOMER_ACCOUNTS` (verified live: `shop.customerAccountsV2.customerAccountsVersion`). `CustomerInput` has no password field at all (verified via schema introspection) — the target system is passwordless by design, not merely "risky to migrate." See `docs/PHASE10_ACCOUNT_STRATEGY.md`. |
| Tags (proposed) | `tags: ["imported-from-woocommerce", "registered"/"guest"]` | Already the pattern used in the pre-existing `shopify_customers_import.csv` |

## 6. Fields never invented

Nothing in this mapping infers a value Shopify doesn't natively support
from a value WooCommerce doesn't provide. Where source data is silent
(shipping address for 90% of customers, marketing consent for 46%,
password for 100%), the Shopify representation is either omitted
entirely or left at platform default — never fabricated.
