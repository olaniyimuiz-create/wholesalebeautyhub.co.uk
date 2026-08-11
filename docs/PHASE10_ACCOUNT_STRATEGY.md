# Phase 10 — Customer Account & Password Strategy

## 1. Verified live (read-only), not assumed

```graphql
{ shop { customerAccounts customerAccountsV2 { customerAccountsVersion
         loginLinksVisibleOnStorefrontAndCheckout loginRequiredAtCheckout url } } }
```

Result, queried against `wholesale-beautyhub.myshopify.com`:

```json
{
  "customerAccounts": "OPTIONAL",
  "customerAccountsV2": {
    "customerAccountsVersion": "NEW_CUSTOMER_ACCOUNTS",
    "loginLinksVisibleOnStorefrontAndCheckout": true,
    "loginRequiredAtCheckout": false,
    "url": "https://shopify.com/85181694208/account"
  }
}
```

This store runs **New Customer Accounts** (Shopify's current-generation
system, hosted at `shopify.com/{shop_id}/account`, not the legacy
`/account` on the store's own domain). This is not a configuration
choice this project made — it's simply what the store already has.

## 2. What this means for passwords — verified, not assumed

Also verified via live schema introspection (`__type(name: "CustomerInput")`):
the `CustomerInput` type used by `customerCreate`/`customerUpdate` has
**no password field of any kind** — not `password`, not
`passwordHash`, nothing. The full field list: `email`, `firstName`,
`id`, `lastName`, `locale`, `metafields`, `note`, `phone`, `tags`,
`emailMarketingConsent`, `smsMarketingConsent`, `whatsAppMarketingConsent`,
`taxExempt`, `taxExemptions`, `multipassIdentifier`.

**Conclusion: WooCommerce password hashes cannot be migrated. This is
not a risk to manage or an assumption to avoid — it is a hard technical
fact about the target platform.** New Customer Accounts are passwordless
by design: every login is a one-time email verification code or magic
link, store-wide, for every customer, with no password ever existing on
either side.

(`multipassIdentifier` exists for Shopify Plus Multipass SSO, which
requires a separate Shopify Plus-only feature and a shared secret — not
applicable here and not investigated further, since nothing in this
project's evidence suggests Shopify Plus or an SSO requirement.)

## 3. Consequence for account activation

Because there is no password step, there is also no traditional
"activation/invitation email before first login" requirement the way
classic Shopify accounts (or WooCommerce) used to work. A customer
created via `customerCreate` can attempt to log in at any time — Shopify
sends them a one-time passcode/magic link to their email at the moment
they try, not before. **No separate activation-email send is required or
recommended as part of the import itself.**

## 4. Consequence for account status preservation

WooCommerce's own account-enabled/disabled distinction (a WordPress user
account can be present but not necessarily "active" in any Woo-specific
sense — this dataset doesn't carry an explicit disabled flag beyond role)
has no meaningful Shopify equivalent to preserve beyond the
`registered`/`guest` tag already planned. A WooCommerce account being
"registered" (has a `wp_users` row) vs. "guest" (checkout-only, no
account) is preserved via the existing tag pattern
(`imported-from-woocommerce`, `registered`/`guest`) — that is the
extent of account-state preservation that is meaningful across the two
systems.

## 5. Business decisions required

None of the above needs a business decision — it's a factual constraint.
What genuinely needs owner input:

1. **Should customers be told their account was migrated** (e.g. a
   one-time email explaining "your account now works differently — no
   password, just enter your email at checkout")? This is a
   communications/CX decision, not a technical one, and this pipeline
   does not send any customer-facing email under any circumstance
   without separate authorization.
2. **Should the "registered" tag distinction be preserved at all**, given
   the underlying account mechanism (password vs. passwordless) is
   completely different now? Recommended: yes, for reporting/segmentation
   value, but this is a preference, not a requirement.
