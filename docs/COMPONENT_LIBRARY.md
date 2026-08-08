# Component Library (Phase 7)

Design system tokens plus every component's status, usage, dependencies,
and known limitations. Status is honest, not aspirational — see
`docs/PHASE7_REPORT.md` § Component Inventory for the summary table this
document backs up. Three states:

- **Built** — real markup, real behavior (JS where needed), reviewed for
  correctness (locale keys, references, JSON all verified — see
  `docs/PHASE7_REPORT.md` § Validation Report). Layout-styled in
  `components.css`.
- **Built, minimally styled** — same correctness bar, but relies on base
  `.card`/`.btn`/`.container` rules rather than dedicated component CSS.
  Functional, not visually polished.
- **Not built this phase** — explicitly out of scope or deferred; listed
  so it isn't mistaken for an oversight.

## Design tokens

All in `assets/theme.css` `:root`, mirrored as merchant-editable settings
in `config/settings_schema.json` where a merchant should reasonably be
able to change them (colors, fonts, container width, grid gap, radius).

| Token group | Examples | Editable in theme editor? |
|---|---|---|
| Color | `--color-accent`, `--color-bg`, `--color-error` | Yes — Colors section |
| Typography | `--font-heading`, `--font-body`, `--text-xs`…`--text-3xl` | Font pickers yes; type scale is fixed (1.25 ratio) |
| Spacing | `--space-1`…`--space-12` (4px base unit) | No — internal consistency, not merchant-facing |
| Radius | `--radius-sm/md/lg/full` | Via `radius_scale` select (sharp/soft/round) |
| Shadow | `--shadow-sm/md/lg` | No |
| Motion | `--duration-*`, `--ease-standard` | No; auto-zeroed under `prefers-reduced-motion` |

## Component inventory

| Component | Status | File(s) |
|---|---|---|
| Design tokens / base styles | Built | `assets/theme.css` |
| Buttons, Cards, Badges, Alerts, Loading/Empty states | Built | `assets/theme.css` |
| Announcement Bar | Built | `sections/announcement-bar.liquid` |
| Header | Built | `sections/header.liquid` |
| Mega Menu | Built | inline in `sections/header.liquid` |
| Mobile Navigation | Built | `snippets/mobile-nav.liquid` |
| Desktop Navigation | Built | inline in `sections/header.liquid` |
| Predictive Search | Built | `snippets/search-drawer.liquid` + `sections/predictive-search.liquid` |
| Search Drawer | Built | `snippets/search-drawer.liquid` |
| Product Card | Built | `snippets/product-card.liquid` |
| Product Grid | Built | inline in `sections/main-collection.liquid`, `sections/featured-collection.liquid` |
| Collection Grid | Built | `sections/main-collection.liquid` |
| Collection Cards | Built | `sections/collection-list.liquid` |
| Brand Cards | Built, minimally styled | brand collections use Product Card + Collection Card; no separate "brand card" markup beyond that |
| Breadcrumbs | Built | `snippets/breadcrumbs.liquid` |
| Pagination | Built | `snippets/pagination.liquid` |
| Filters | Built | `snippets/facets.liquid` |
| Sort Controls | Built | `snippets/sort-control.liquid` |
| Footer | Built | `sections/footer.liquid` |
| Newsletter | Built | `sections/newsletter.liquid` (standalone) + inline block in `sections/footer.liquid` |
| Brand Banner | Built, minimally styled | `sections/brand-banner.liquid` |
| Collection Banner | Built, minimally styled | `sections/collection-banner.liquid` |
| Promo Banner | Built, minimally styled | `sections/promo-banner.liquid` |
| Hero Banner | Built, minimally styled | `sections/hero-banner.liquid` |
| Image Gallery / Product Media | Built | `snippets/product-media.liquid` |
| Variant Picker | Built | `snippets/variant-picker.liquid` |
| Swatches | Built (text-pill, not color) | see note below |
| Sticky Cart (add-to-cart bar) | Built | `snippets/sticky-add-to-cart.liquid` |
| Mini Cart / Cart Drawer | Built | `snippets/cart-drawer.liquid` |
| Cart page | Built, minimally styled | `sections/main-cart.liquid` |
| Related Products / Cross-sell | Built | `snippets/product-recommendations.liquid` (Shopify native recommendations API covers both) |
| Recently Viewed | Built | `snippets/recently-viewed.liquid` (client-side, localStorage) |
| Account Navigation | Built, minimally styled | `snippets/account-nav.liquid` |
| 404 | Built | `sections/404.liquid` |
| Blog Cards / Article Cards | Built | `snippets/article-card.liquid` |
| FAQ Accordion | Built | `sections/faq.liquid` + `blocks/faq-item.liquid` (Theme Block) |
| Trust Badges | Built | `blocks/trust-badge.liquid` (Theme Block) |
| Quick Add | Built (trigger only) | button + data hooks exist in `product-card.liquid`; no modal/flyout content is wired up — see Known Limitations |
| Quick View | **Not built this phase** | scope decision — see below |
| Testimonials | **Not built this phase** | no source content exists yet (not in the WooCommerce data); build once copy exists |
| Customer account pages beyond login/register/order history | **Not built this phase** | `templates/customers/addresses.liquid` etc. not created |

### Why Quick View and full Quick Add weren't built

Quick View (a modal showing full product detail without navigating) and a
complete Quick Add flyout (variant selection inside a popover) both
duplicate most of the product page's own logic (variant picker, media,
price) behind a second implementation surface. Building a *shallow* version
would violate `docs/SHOPIFY_BUILD_GUIDELINES.md`'s "zero technical debt"
principle — a Quick View that only shows title/price/one image isn't what
that component means. Building the *full* version is real additional scope
better spent once real usage data (Phase 12) shows whether customers
actually want it, rather than speculatively. Quick Add's trigger and data
attributes exist in `product-card.liquid` specifically so this can be
wired up later without touching the card markup again.

### Swatches: text pills, not color swatches

This catalog's shade/color option values are names like "Cape Town 05" and
"Doha 02" — not CSS colors, and no swatch-color or swatch-image resource
is defined in Shopify for them yet (that requires a merchant-configured
metafield mapping each value to a real color/image, which doesn't exist).
Rendering `style="background: Cape Town 05"` would silently fail. The
built version renders readable text pills instead, which is correct given
the actual data; upgrading to true color swatches is a data task (define
the swatch metafield, map ~127 shade names to colors), not a theme task,
and shouldn't block Phase 7.

## Flagship component detail

### Product Card (`snippets/product-card.liquid`)

- **Usage**: `{% render 'product-card', product: product, loading: 'lazy' %}`
- **Dependencies**: `snippets/price.liquid`, `snippets/icon.liquid`,
  `settings.show_vendor_on_card` / `show_quick_add` / `enable_swatch_preview`
- **Performance notes**: first 8 cards in a collection grid render with
  `loading: 'eager'` (see `main-collection.liquid`), the rest lazy. Hover
  image swap uses a second `<img>` with opacity transition, not a JS-driven
  image swap, so it works before JS loads.
- **Accessibility notes**: media link is `aria-hidden`/`tabindex="-1"` when
  Quick Add is shown (avoids a duplicate unlabeled link landing target for
  screen readers/keyboard nav — the title link already covers navigation).
- **Known limitations**: swatch preview is text-pill only (see above).

### Variant Picker (`snippets/variant-picker.liquid`)

- **Usage**: `{% render 'variant-picker' %}` inside a product form; expects
  `product` in scope.
- **Dependencies**: `assets/theme.js` (`[data-variant-picker]` handler) —
  without JS, the radio inputs are visible/selectable but don't update
  price/media/availability; the form still submits the originally-selected
  variant on Add to Cart, so purchasing still works, just without live
  preview.
- **Performance notes**: price/media updates after a selection re-fetch
  `?section_id=main-product` rather than reimplementing Shopify's money
  formatting in JS — one extra request per selection change, traded
  deliberately against risking a formatting bug that drifts from the
  shop's actual currency settings.
- **Accessibility notes**: each option group is a `<fieldset>`/`<legend>`;
  selected value announced via the legend text update.
- **Known limitations**: single/simple option-set tested against this
  catalog's real data (max 1 option, "Shade"); the 3-option code path is
  written generically but unverified against real 2–3 option data since
  none exists in this catalog.

### Cart Drawer (`snippets/cart-drawer.liquid`)

- **Usage**: rendered once in `layout/theme.liquid`; also fetchable via
  `sections/cart-drawer-fragment.liquid` for post-update refresh.
- **Dependencies**: Shopify Ajax Cart API (`/cart.js`, `/cart/add.js`,
  `/cart/change.js`) — no app, all native.
- **Performance notes**: refetches the whole drawer fragment after any
  cart mutation rather than patching the DOM incrementally — simpler and
  correct, costs one extra request per cart action.
- **Accessibility notes**: `role="dialog"`, focus-trapped, Escape closes,
  focus returns to the opening trigger.
- **Known limitations**: no optimistic UI (quantity doesn't update until
  the server round-trip completes) — deliberate, avoids showing a cart
  total that doesn't match what checkout will actually charge.

### Facets / Filters (`snippets/facets.liquid`)

- **Usage**: `{% render 'facets', section_id: section.id %}` inside
  `main-collection.liquid`.
- **Dependencies**: native `collection.filters` (Search & Discovery) — no
  app. Omitted entirely if `collection.filters.size == 0`.
- **Performance notes**: filtering re-fetches `?section_id=main-collection`
  and swaps just the grid, not a full page reload.
- **Accessibility notes**: `<fieldset>`/`<legend>` per filter group,
  disabled checkboxes for zero-result values (not hidden — keeps the
  option discoverable).
- **Known limitations**: works without JS (plain form GET), but the
  Vendor filter's absence on brand collection pages relies on Shopify
  omitting single-value filters automatically — not verified against a
  live store since none exists yet (see `docs/PHASE7_REPORT.md` §
  Validation Report for what's verified vs. assumed).
