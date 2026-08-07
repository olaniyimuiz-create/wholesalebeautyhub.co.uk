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
| 5 | Category "VALENTINE COMBO DEALS" and brand "VALENTINE COMBO DEALS" share a slug, both mapping to `/collections/valentine-combo-deals` | Medium | Open | Must be resolved before Phase 6 collection creation — rename one handle or merge them (`docs/SEO_STRATEGY.md` finding #1) |
| 6 | Site navigation is partly theme/page-builder-driven (footer, WooCommerce account UI), not fully represented in `wp_nav_menu` data | Medium | Open | Phase 6/7 navigation rebuild must be checked against the live site directly, not assumed from the database alone (`docs/SEO_STRATEGY.md`) |
| 7 | "Contact" and "Cookie Policy" pages have no discoverable inbound link (menu, content, or footer) on the live site | Low | Open | Needs a human check on the live site before cutover; not blocking |
| 8 | Live Cookie Policy page has an incorrect meta description (copy-pasted from Terms & Conditions) — pre-existing WooCommerce-side bug, not caused by migration | Low | Open | Fix when porting page copy to Shopify (Phase 6/7) rather than carrying the error forward |
