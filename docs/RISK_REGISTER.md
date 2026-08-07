# Risk Register

Moved out of `docs/MIGRATION_PROGRESS.md` (Phase 5) so risk tracking doesn't
live inside a changelog. Update in place as risks are mitigated or new ones
surface; don't delete resolved rows, mark them Resolved with how.

| # | Risk | Impact | Status | Mitigation |
|---|---|---|---|---|
| 1 | Customer/order PII committed to a public repo | High | Mitigated | `.gitignore` covers `dump.sql`, `migration/data/*.json`, and the generated import CSVs; verify `git status` before every commit |
| 2 | Shopify CSV import silently drops/misreads a field because Shopify's format changed since implementation | Medium | Open | Re-verify column headers against `help.shopify.com` before each real import, not from memory |
| 3 | SEO loss from URL structure change (WooCommerce slugs vs Shopify) | High | Mitigated (Phase 5) | Full redirect matrix built and validated (`reports/redirect_matrix.csv`, 875 URLs); remaining exposure is the tag→`/collections/all` fallback (ADR-003) and the brand/category slug collision (see below) |
| 4 | Order history isn't importable via product/customer CSV | Medium | Open | Needs a dedicated decision in Phase 11 (Matrixify vs. Transporter vs. Admin API vs. accept the gap) |
| 5 | Category "VALENTINE COMBO DEALS" and brand "VALENTINE COMBO DEALS" share a slug, both mapping to `/collections/valentine-combo-deals` | Medium | Resolved (Phase 6) | Root cause found: it's a mislabeled seasonal bundle promo, not a real brand (8 products, all gift sets). Reclassified as a manual collection and dropped from the brand/Vendor system — ADR-007. Same fix applied to "TRADEFAIR COMBO DEALS" pre-emptively. |
| 6 | Site navigation is partly theme/page-builder-driven (footer, WooCommerce account UI), not fully represented in `wp_nav_menu` data | Medium | Mitigated (Phase 6 design) | Shopify footer will be a real navigation menu (`docs/SHOPIFY_ARCHITECTURE.md` § Navigation), not theme-hardcoded — fixes the blind spot instead of reproducing it. Still needs Phase 7 build-out. |
| 7 | "Contact" and "Cookie Policy" pages have no discoverable inbound link (menu, content, or footer) on the live site | Low | Open | Included in the planned Shopify footer menu regardless (`docs/SHOPIFY_ARCHITECTURE.md`); their absence today is treated as a defect being fixed, not preserved |
| 8 | Live Cookie Policy page has an incorrect meta description (copy-pasted from Terms & Conditions) — pre-existing WooCommerce-side bug, not caused by migration | Low | Open | Fix when porting page copy to Shopify (Phase 6/7) rather than carrying the error forward |
| 9 | `csv_generator.py`'s `Type` column uses the first assigned category, not the most specific one — doesn't match the Product Type strategy in `docs/SHOPIFY_ARCHITECTURE.md` (ADR-009) | Medium | Open | Fix in Phase 9 when the product CSV is actually regenerated for import — Phase 6 is architecture-only and doesn't touch import scripts per this phase's explicit scope |
| 10 | ~12 WooCommerce categories are miscategorized at top level (should be Level 2/3 content) and need merchant sign-off before becoming Shopify collections | Low | Open | Proposed mapping in `docs/SHOPIFY_ARCHITECTURE.md` § Category hierarchy; needs a human decision, not automatable |
| 11 | The 8 reclassified bundle products (ADR-007) need a manual per-product check for their real Vendor value | Low | Open | Not automated — guessing a manufacturer from a bundle listing risks being wrong. Manual review before Phase 9 |
| 12 | 37 of 166 `pwb-brand` terms have zero published products | Low | Informational | No action needed — Vendor/brand-collection creation in Phase 9 naturally skips brands with no products, since it's driven by what's actually on live products |
| 13 | Product tag casing/naming is inconsistent (e.g. "MASCARA" vs lowercase tags elsewhere) | Low | Open | Needs a normalization pass before tags are used as Search & Discovery filters (`docs/SHOPIFY_ARCHITECTURE.md` § Search & Discovery filters) |
