# Shopify Foundation Layer (Phase 6.5)

Takes the approved Phase 6 architecture (`docs/SHOPIFY_ARCHITECTURE.md`,
ADR-006 through ADR-009) and makes it concrete and buildable: exact
collection list, exact metafield/metaobject schema, real plugin→app
mapping, real asset counts. This is the "what to build" spec; the "how to
ship it" process lives in `docs/SHOPIFY_DEPLOYMENT.md`,
`docs/SHOPIFY_BUILD_GUIDELINES.md`, and `docs/SHOPIFY_CODING_STANDARDS.md`.

Machine-readable versions of the collection/metafield/metaobject/navigation
specs below live under [`/shopify/foundation/`](../shopify/foundation/) for
Phase 9 tooling to consume directly.

No theme code, imports, or production data changes happen in this phase.

## Information model

How WooCommerce's data model becomes Shopify's:

| WooCommerce concept | Shopify concept | Notes |
|---|---|---|
| `wp_posts` (post_type=product) | Product | 1:1, existing pipeline (`database_parser.py`) |
| `wp_posts` (post_type=product_variation) | Variant | 1:1, up to 3 options per Shopify limit — this catalog's max is 1 (shade/size), no conflict |
| `product_cat` term (Level 2) | Collection (rule-based) | ADR-009 |
| `product_cat` term (Level 3) | Product Type (field value) | ADR-009 |
| `product_cat` term (Level 1) | Navigation structure only | ADR-009 — no Shopify object |
| `pwb-brand` term | Vendor (field) + Collection (rule-based) + Metaobject (fast-follow) | ADR-006 |
| `product_tag` term | Tag | 1:1, free text |
| `wp_users` + `wp_wc_customer_lookup` | Customer | 1:1, existing pipeline |
| `wp_posts` (post_type=page) | Page | Phase 8/9 scope |
| `wp_posts` (post_type=post) | Blog article | Phase 8/9 scope, `news` blog handle (ADR-004) |
| `wp_posts` (post_type=attachment) | Product image / File | Phase 8 scope |
| `wp_options` (`woocommerce_currency`, `_weight_unit`) | Shop settings | Already read into `database_parser.py`/`csv_generator.py` constants |
| WooCommerce orders (`wp_wc_orders` etc.) | Order (historical) | Not covered by CSV import — Phase 11 decision |

## Collection architecture (concrete)

Counts are live product counts from `migration/data/products.json` as of
this phase; re-verify before Phase 9 import since the catalog changes.

### Category-driven collections (23 already correctly nested + 5 promoted from the approved cleanup)

| Group | Collection | Handle | Products |
|---|---|---:|---:|
| Makeup | Face | `face` | 91 |
| Makeup | Eyes | `eyes` | 60 |
| Makeup | Lips | `lips` | 27 |
| Makeup | Brushes & Beauty Blenders | `brushes-beauty-blenders` | 15 |
| Skin Care | Serums & Treatment | `serums-treatment` | 89 (+ merged, see below) |
| Skin Care | Moisturizers & Cream | `moisturizers` | 63 |
| Skin Care | Face Cleansers & Wash | `face-cleansers-wash` | 43 |
| Skin Care | Face Toners, Mist & Essence | `face-toners-mist-essence` | 37 |
| Skin Care | Sunscreen | `sunscreen` | 32 |
| Skin Care | Brightening | `brightening` | 32 |
| Skin Care | Hyperpigmentation | `hyperpigmentation` | 21 |
| Skin Care | Exfoliators, Peels & Scrubs | `exfoliators-peels-scrubs` | 20 |
| Skin Care | **Acne Treatment** (promoted) | `acne-treatment` | 25 |
| Skin Care | Mask | `face-mask` | 4 |
| Skin Care | Face Oil | `face-oil` | 1 |
| Bath & Body Care | Body Lotion | `body-lotion` | 63 |
| Bath & Body Care | Body Wash | `body-wash` | 54 |
| Bath & Body Care | Body Moisturizers | `body-moisturizers` | 34 |
| Bath & Body Care | Body Oil | `body-oil` | 18 |
| Bath & Body Care | **Body Care** (Body Body, promoted, needs a real name — "Body Body" isn't a usable collection title) | `body-care` | 18 |
| Bath & Body Care | Body Scrubs | `body-scrubs` | 7 |
| Bath & Body Care | **Body Butter** (promoted) | `body-butter` | 9 |
| Bath & Body Care | **Baby Wash & Lotion** (promoted) | `baby-wash-lotion` | 8 |
| Bath & Body Care | Roll On | `roll-on` | 2 |
| Bath & Body Care | **Body Mist** (promoted) | `body-mist` | 2 |
| Bath & Body Care | Cotton Pad | *(skip — 0 products)* | 0 |
| Beauty Tools | Tools & Accessories (absorbs "sponge", 1 product) | `tools-accessories` | 23 |

Merged into Serums & Treatment rather than kept separate (per the approved
cleanup): Glow serum (17), Dark spots & Discoloration serums (42), Eye
cream (3), Glow spray (4) — some product overlap with the existing 89 is
expected; get the deduplicated count at Phase 9 import time, not from
summing these.

**Not carried forward**: "Uncategorized" (4 products) — needs a manual
audit of which 4 products these are and what their real category should
be, not a 1:1 collection. Flagged in the risk register.

### Brand-driven collections (127)

129 `pwb-brand` terms have ≥1 published product; 2 are removed per ADR-007
(Valentine/Tradefair reclassification), leaving **127** real brand
collections, rule: `Vendor equals {brand name}`. Top 10 by product count:
Olay (28), Eos (26), Zaron (25), Naturium (22), Good Molecules (20),
Hegai & Esther (16), Msmetics (15), Vee Beauty (15), Cosrx (13),
Dr Teal's (13). Full list: [`shopify/foundation/collections.json`](../shopify/foundation/collections.json).

Two data-quality issues found while compiling this list (neither blocks
Phase 6.5, both need a Phase 9 decision — added to the risk register):

- One brand term has name "Accessories _make_up" and slug `unnamed` (5
  products) — clearly a data-entry error, not a real brand name. Needs
  manual identification of what these 5 products' actual brand/vendor
  should be before it becomes a Vendor value.
- Three separate brand terms are almost certainly the same brand entered
  with inconsistent spelling: "Topicrem" (2 products), "Tropicrem" (1),
  "Topicream" (1). Recommend consolidating to one Vendor value
  ("Topicrem") before Phase 9 rather than creating 3 near-duplicate brand
  collections for 4 products total.

### Manual (curated) promo collections (3)

Per ADR-007 — not rule-based, not part of the Vendor/brand system, hand-
picked and updated seasonally:

| Collection | Handle | Source (WooCommerce) | Products |
|---|---|---|---:|
| Valentine Combo Deals | `valentine-combo-deals` | `product_cat` term (was also wrongly tagged as a brand — dropped) | 8 |
| Tradefair Combo Deals | `tradefair-combo-deals` | `product_cat` term "TRADEFAIR COMBO DEAL" (was also a separate, wrongly-tagged brand term with a near-duplicate name and 11 products — consolidate under this one collection) | 20 |
| Combo Deals | `combo-deals` | `product_cat` term (general bundle/gift-set collection) | 17 |

**Total planned collections: ~158** (23 existing Level 2 + 5 promoted +
127 brand + 3 manual promo).

## Navigation structure (concrete)

```
Header:
  Home
  Makeup ▾        → Face, Eyes, Lips, Brushes & Beauty Blenders
  Skin Care ▾      → Serums & Treatment, Moisturizers & Cream, Face Cleansers
                     & Wash, Face Toners/Mist & Essence, Sunscreen,
                     Brightening, Hyperpigmentation, Exfoliators/Peels &
                     Scrubs, Acne Treatment, Mask, Face Oil
  Bath & Body Care ▾ → Body Lotion, Body Wash, Body Moisturizers, Body Oil,
                        Body Care, Body Scrubs, Body Butter, Baby Wash &
                        Lotion, Roll On, Body Mist
  Beauty Tools ▾   → Tools & Accessories
  Brands ▾         → curated top ~10 brands (by product count, see table
                     above) + "View all brands" link
  (optional) Deals → Valentine Combo Deals, Tradefair Combo Deals,
                     Combo Deals — recommend adding this as a 7th top-level
                     item since promo collections have no natural home in
                     the taxonomy-driven dropdowns above; merchant call

Footer (real Shopify menu this time — Phase 5/6 found the WooCommerce
footer wasn't menu-driven at all):
  About | Contact | Privacy Policy | Terms & Conditions | Shipping Policy |
  Refund & Returns Policy | Cookie Policy
```

Machine-readable: [`shopify/foundation/navigation.json`](../shopify/foundation/navigation.json).

## Metafield definitions (concrete)

| Namespace.Key | Type | Applies to | Purpose |
|---|---|---|---|
| `custom.brand` | Metaobject reference (`brand`) | Product | Links to rich brand content (fast-follow, ADR-006) |
| `custom.included_items` | List of single-line text | Product | "What's included" block for combo/bundle products (`product.combo-deal.json`) |
| `custom.legacy_woo_id` | Single-line text (integer) | Product, Customer | Original WooCommerce post/user ID — keep for support/audit traceability during and after cutover, cheap to add now |

Full definitions (validation rules, descriptions):
[`shopify/foundation/metafields.json`](../shopify/foundation/metafields.json).

## Metaobject definitions (concrete)

`brand` metaobject (fast-follow per ADR-006 — schema defined now so Phase 7
theme work can build against a stable contract):

| Field | Type | Required |
|---|---|---|
| `name` | Single line text | Yes |
| `logo` | File (image) | No |
| `hero_image` | File (image) | No |
| `description` | Rich text | No |
| `founded_year` | Integer | No |
| `website` | URL | No |
| `seo_title` | Single line text | No |
| `seo_description` | Single line text | No |

Handle must match the paired brand collection's handle exactly (enforced
by convention, not the platform — note it in the Phase 9 import runbook).
Full schema: [`shopify/foundation/metaobjects.json`](../shopify/foundation/metaobjects.json).

## Product taxonomy (concrete)

Product Type = the most specific WooCommerce category assigned (Level 3
where one exists, otherwise Level 2) — e.g. "Foundation", "Body Lotion".
Known gap: `csv_generator.py` currently sets `Type` from `categories[0]`
(first in list order), not the most specific one — tracked as risk #9,
fixed in Phase 9 when the CSV is actually regenerated, not in this
documentation-only phase.

## Search & Discovery filter design (concrete)

Enabled on category and brand collection pages: **Vendor** (omitted on
brand collection pages — redundant, single-vendor by definition),
**Price**, **Availability**, **Product Type**, and a curated subset of
**Tags** for concern/ingredient filtering. Tag filtering is deferred until
the casing/naming normalization pass (risk #13) — filtering on
inconsistent tags (`MASCARA` vs `mascara`) would silently split one
logical filter value into two.

## Asset inventory

From the WooCommerce media library (`wp_posts` post_type=attachment):

| | Count |
|---|---:|
| Total media library attachments | 1,719 |
| Distinct images actually referenced by published products (main + gallery + variant) | 1,123 |
| Published products with zero images | 4 |
| JPEG | 1,110 |
| WebP | 440 |
| PNG | 128 |
| AVIF | 14 |
| HEIC | 14 |
| SVG | 7 |
| Non-image files in the media library (HTML/CSV/MP4) | 5 |

Format notes (checked against Shopify's current product-media
requirements, not assumed): JPEG, PNG, WebP, HEIC, and SVG are all
natively supported for product images — no conversion needed for those.
**AVIF is not on Shopify's supported product-image format list** — the 14
AVIF files need conversion (to WebP or JPEG) before upload. The 1 MP4 file
should be reviewed individually — Shopify supports product video, so it
may be usable as-is rather than dropped. The HTML/CSV files in the media
library aren't product images and should be excluded from the media
migration entirely.

Media migration itself (re-hosting 1,123+ images on Shopify's CDN) is
Phase 8 scope — this inventory exists so Phase 8 starts with real numbers
instead of discovering them mid-migration.

## App replacement matrix

Built from `wp_options.active_plugins` — the actual list of plugins
running the live site today, not inferred from leftover database tables
(several tables exist for plugins that are no longer active — see note at
the end).

| WooCommerce/WordPress plugin | Function | Shopify replacement |
|---|---|---|
| woocommerce | Core commerce platform | Shopify itself |
| perfect-woocommerce-brands | Brand taxonomy | Native Vendor field + Smart Collections (ADR-006) — this migration's core Phase 6 decision |
| rey-core (Rey theme) | Storefront theme | New Shopify theme (Phase 7) |
| woo-stripe-payment | Payment processing | Shopify Payments (Stripe-powered natively) |
| weight-based-shipping-for-woocommerce | Shipping rates | Native Shopify shipping profiles (Settings → Shipping) |
| ajax-search-for-woocommerce-premium | Storefront search | Shopify Search & Discovery (native) |
| back-in-stock-notifier-for-woocommerce | Restock alerts | App needed (e.g. Back in Stock / Swym) |
| buy-again-for-woocommerce | Reorder from account | App or theme customization — no native equivalent |
| woo-cart-abandonment-recovery | Abandoned cart emails | Native Shopify abandoned checkout emails (Settings → Checkout); SMS follow-up needs an app |
| woo-checkout-field-editor-pro | Custom checkout fields | Shopify Plus Checkout Extensibility/Functions, or "Additional checkout fields" |
| woocommerce-bulk-editor | Bulk product edits | Native Shopify bulk editor / Admin API |
| woocommerce-pdf-invoices-packing-slips | Invoices/packing slips | App (e.g. Order Printer) |
| woocommerce-photo-reviews / reviews-feed | Product reviews (incl. photos) | Review app (e.g. Judge.me, Loox) — one app covers both |
| perfect-woocommerce-brands *(again — brand pages)* | see above | — |
| webappick-product-feed-for-woocommerce | Google Shopping feed | Native Google & YouTube channel app |
| revslider | Homepage/hero sliders | Native Shopify theme sections (Online Store 2.0) |
| elementor | Page builder | Native Shopify theme customizer/sections; app (PageFly/GemPages) only if truly complex layouts are needed |
| fluentform | Contact/lead forms | Native contact form section, or a form app |
| fluent-crm / fluentcampaign-pro | Email marketing/CRM | Shopify Email or an ESP app (Klaviyo etc.) |
| official-mailerlite-sign-up-forms | Email signup forms | Same ESP decision as above — **note**: both FluentCRM and MailerLite are active simultaneously today; worth asking the client which is the real system of record rather than migrating both |
| duracelltomi-google-tag-manager / google-site-kit | Analytics/GTM | Native Shopify Google & YouTube integration, GA4 |
| wp-whatsapp | WhatsApp chat widget | WhatsApp chat app, or Shopify Inbox if WhatsApp channel is available in-region |
| wp-all-export | Data export (used for this migration) | Not needed post-migration — this project's own pipeline supersedes it |
| code-snippets | Custom PHP snippets | Content unknown from the DB alone — needs a manual read of what's in `wp_snippets` before Phase 7, in case any snippet implements storefront behavior that needs a Liquid/Shopify Function equivalent |
| fluent-smtp | Transactional email delivery | Not needed — Shopify sends transactional email natively |
| object-cache-pro, wp-file-manager, wp-mail-logging, user-switching, user-role-editor | Server/admin infrastructure | Not applicable — Shopify is hosted, no equivalent needed |
| ai-provider-for-anthropic, ai-provider-for-google | AI integration (purpose unclear from DB) | Needs a manual check of what these power before deciding on a Shopify equivalent (Shopify Magic, or a support/chat app) |

**Inactive-plugin data still in the database** (tables exist but the
plugin isn't in the current `active_plugins` list — no Shopify replacement
needed unless the client confirms one of these is still actually in use
via a mechanism this check wouldn't catch, e.g. a must-use plugin):
Wordfence security (`wp_wf*` — extensive: audit log, blocked IPs, login
history), eBay integration (`wp_ebay_*`), Zoho Books/CRM sync
(`wp_vxc_zoho_*`, `wp_wps_woo_zoho_*`), Google Listings & Ads
(`wp_gla_*`), accessibility scanner (`wp_ea11y_*`), cookie consent banner
(`wp_cky_*`), point-of-sale (`wp_apbd_pos_*` — **worth explicitly
confirming with the client**, since in-store POS would need Shopify POS as
a real replacement, not a "not applicable"), dynamic pricing/discount
rules (`wp_wdp_*`, `wp_wdr_*`), Instagram/Facebook feed widgets
(`wp_sbi_*`, `wp_cff_*`), image optimization (`wp_smush_*`).

## Import sequence & rollback strategy

Moved to [`docs/SHOPIFY_DEPLOYMENT.md`](SHOPIFY_DEPLOYMENT.md) to avoid
duplicating operational detail across two documents — this file stays
focused on *what* gets built, that one covers *how and in what order*.
