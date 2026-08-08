# Media Migration (Phase 8)

Analysis, inventory, and validation only — no files were uploaded to
Shopify, no WooCommerce data was modified. Every number in this document
comes from `migration/scripts/media_inventory.py` actually running against
`migration/sql/dump.sql`, re-verified twice for idempotency (see § 16),
not copied from Phase 6.5's earlier estimate.

## 1. Purpose

Build a canonical, deterministic, traceable media inventory the eventual
Phase 9 import can consume without re-deriving it, and resolve — with real
data, not the assumption Phase 7's review made from reading plugin code —
whether `wp_termmeta.pwb_brand_image` is a real source to migrate.

## 2. Source systems

`migration/sql/dump.sql` (Adminer export) is the only source consulted.
No live HTTP crawl of the WooCommerce site was performed for inventory
purposes; a small (n=6) spot-check against the live site's actual files
was done afterward purely to sanity-check the database-derived metadata
(§ 16), not to build the inventory itself.

## 3. Media sources inspected

| Source | Table(s) | What it gives |
|---|---|---|
| Attachment posts | `wp_posts` (post_type=attachment) | File path, mime type, title |
| Attachment file/size/dimensions | `wp_postmeta` (`_wp_attached_file`, `_wp_attachment_metadata`) | Real width/height/filesize **without downloading any file** — WordPress stores this at upload time |
| Attachment alt text | `wp_postmeta` (`_wp_attachment_image_alt`) | Only 3 of 1,719 attachments have this set — negligible coverage, see § 14 |
| Product featured/gallery images | `wp_postmeta` (`_thumbnail_id`, `_product_image_gallery`) on `product` posts | |
| Variant images | `wp_postmeta` (`_thumbnail_id`) on `product_variation` posts | |
| Category thumbnails | `wp_termmeta` (`thumbnail_id`) on `product_cat` terms | |
| Brand logos | `wp_termmeta` (`pwb_brand_image`) on `pwb-brand` terms | **Investigated exhaustively — see § 5** |
| Product/page/blog body images | `<img>` tags in `post_content` | Regex-scanned, resolved back to attachment records where possible |
| Site identity | `wp_options` (`site_logo`, `site_icon`) | |

## 4. Inventory methodology

Streamed once through the dump per source type (reusing
`migration/scripts/sql_utils.py`, the same parser every other pipeline
script uses), building one record per (source, usage) pair rather than
one record per physical file — a single image used as both a product's
featured image and in its gallery produces two records, correctly linked
by the same `attachment_id`, so relationship coverage is queryable without
losing the physical-file/usage distinction (see § 9, SHARED vs.
DUPLICATE). `hash` in every report is a **metadata fingerprint**
(SHA-256 of lowercased filename + filesize), not a binary content hash —
no image bytes were downloaded at inventory scale (1,719 files against
production would be excessive load for a DB-answerable question). This
can prove two records are *definitely different* (different fingerprint)
but can only suggest, not prove, that two records are the *same physical
bytes* (matching fingerprint) — flagged as `DUPLICATE_CANDIDATE`, not
auto-merged. See § 9.

## 5. Brand logo methodology — the mandatory investigation

**Finding: no brand logo data exists anywhere in this database.**

Phase 7's acceptance review found a WordPress snippet
(`wp_snippets`, "WBH - Products by Category & Brand Logos Shortcodes")
that reads `get_term_meta($brand->term_id, 'pwb_brand_image', true)` and
concluded brand logos must live at that meta key. That was a read of the
snippet's *source code*, not the *data* — this phase checked the data:

```
wp_termmeta rows for meta_key = 'pwb_brand_image':        0
wp_termmeta rows of ANY kind for ANY of the 166 pwb-brand terms: 0
```

Every one of the 166 `pwb-brand` taxonomy terms has **zero** rows in
`wp_termmeta` — not just missing the specific key, but no term-level
metadata of any kind. The shortcode's own code has a fallback path for
exactly this case (`if ($logo_url) {...} else { text-only card }`) — that
fallback is what has always rendered on the live site. There was never a
brand-logo upload feature actually used, despite the code existing to
support one.

**Conclusion for Phase 9**: the `brand` metaobject's `logo` field
(`shopify/foundation/metaobjects.json`) will start empty for all 127
planned brand collections. This isn't a migration gap — there is nothing
to migrate. Logos need to be sourced fresh (manufacturer press kits,
brand websites) and uploaded directly in Shopify Admin as part of
building out the brand metaobject content layer (ADR-006's fast-follow
phase), not through this pipeline.

**Bonus finding from the same investigation** (not part of the media
inventory — it's color data, not image files, so out of Phase 8's scope,
but worth recording since it directly affects a Phase 7 limitation): all
332 `pa_shade`/`pa_color`/`pa_shades` attribute terms have a real,
populated hex color value in `wp_termmeta.rey_attribute_color` (e.g.
"Cape Town-05" → `#e0a270`). `rey_attribute_image` (an actual swatch
*image* field) exists as a column but is empty for all 332 — no swatch
images exist, matching the brand-logo pattern of "the field exists, the
data was never entered." But the *color* data is real and complete. This
means `docs/COMPONENT_LIBRARY.md`'s Phase 7 finding that swatches must be
text-pills "since no swatch-color... resource is defined" was **incorrect
for color** (it's correct that no swatch *image* exists) — real hex
colors exist and were not used. Flagged as a new risk (§ 19) and a new
issue, not fixed here since it's a theme-code change, not media migration.

## 6. Product media methodology

For each published product (`migration/data/products.json`, regenerated
fresh immediately before this inventory ran — not reused from an earlier
session): `_thumbnail_id` → `product_featured`, each ID in
`_product_image_gallery` → `product_gallery`. A real WordPress quirk
handled explicitly: `_thumbnail_id`/gallery entries of literal `'0'` mean
"not set," not "attachment ID zero" — the first version of this script
mishandled that (see § 16, caught by re-verification) and produced 148
false "missing" variant images and misclassified all 46 category
thumbnail slots as broken before the fix.

## 7. Variation media methodology

Same `_thumbnail_id` mechanism, scoped to `product_variation` posts,
linked back to the parent product via `products.json`'s existing
parent/variation structure — no assumption that WooCommerce IDs will
equal future Shopify IDs; the manifest keys everything by the stable
WooCommerce source ID (§ 11).

## 8. CMS media methodology

Page and blog post `post_content` (published only) and product
`body_html` scanned for `<img src="...">` pointing at
`/wp-content/uploads/...`. Resolved back to an attachment record by exact
`_wp_attached_file` match first, then by filename with any WordPress
image-size suffix (`-600x600`, etc.) stripped, since content images are
frequently a specific generated size rather than the original file.
8 references couldn't be resolved this way — see § 10.

Category *term descriptions* were checked for embedded `<img>` tags and
found to contain none (all `product_cat`/`pwb-brand` term descriptions in
this dataset are empty or plain text).

## 9. Duplicate handling

368 of 380 flagged pairs are **SHARED** — the same `attachment_id` doing
double duty (e.g. a product's featured image also appearing as gallery
position 1). That's normal and expected, not a problem to fix. The
remaining **12 are DUPLICATE_CANDIDATE** — different `attachment_id`s
that happen to share filename + filesize. Per the metadata-fingerprint
limitation in § 4, these are candidates for a human/binary-hash check
before Phase 9, not auto-merged or auto-deleted — shared media must never
be treated as accidentally-duplicated media (`reports/media_duplicates.csv`
carries the classification for every row so this distinction isn't lost
downstream).

## 10. Missing media handling

9 genuinely unresolved references (down from an initial false count of
203 before the `'0'`-handling fix, see § 16):

| Type | Count | Detail |
|---|---:|---|
| `site_identity` (site logo) | 1 | `wp_options.site_logo` points at an attachment ID with no matching post — likely from a since-deleted/replaced logo upload |
| Content-embedded images (pages + product descriptions) | 8 | `<img>` tags referencing files with no matching `wp_posts`/`_wp_attached_file` record — either uploaded outside the standard media-library flow, or genuinely deleted since being embedded |

Full detail: `reports/media_missing.csv`. All 9 are `REQUIRES_REVIEW`,
not auto-resolved.

## 11. Product/variation relationship coverage

`reports/media_relationships.csv` maps every media record to its
WooCommerce `product_id`/`variation_id`/`term_id` — never a future
Shopify ID, since Phase 9 (product import) hasn't happened. 906 product-
level image usages (688 featured + 218 additional gallery positions) and
363 variant-level image usages, both keyed to the same stable source IDs
`migration/data/products.json` already uses, so Phase 9's product import
and this media manifest can be joined on `product_id`/`variation_id`
without a separate ID-mapping step.

## 12. AVIF conversion

**14 AVIF attachments confirmed** — independently recounted this phase
via a fresh `wp_posts.post_mime_type` scan (not reused from Phase 6.5;
happens to match exactly, which is a consistency check passing, not an
assumption). Listed individually with real width/height in
`reports/media_conversion.csv`. **No conversion was performed** — this
phase is inventory and validation, not execution; `conversion_status` is
`NOT_STARTED` for all 14, ready for whichever tool actually re-encodes
them (e.g. during upload) to consume.

## 13. Supported formats

Re-verified against Shopify's currently-documented product-media formats
(not memory): JPEG, PNG, GIF, BMP, TIFF, PSD, SVG, HEIC, WebP are all
supported — so the 14 HEIC and 7 SVG files need **no** conversion, only
AVIF does. Also found: 1 BMP (supported, no action), 1 MP4 (Shopify
supports product video — not a conversion case), and **3 HTML + 1 CSV
files sitting in the media library** — not images at all, almost
certainly accidental (e.g. an exported report or a saved webpage uploaded
by mistake). Flagged in `reports/media_unsupported.csv` for human review
("does this belong in the media library at all"), not for conversion.

## 14. Filename strategy

Deterministic, reproducible pattern applied to every record's
`target_filename`:

```
{source_type}-{stable_id}-{slugified_title}[-{index}].{target_format}
```

- `stable_id` is the WooCommerce attachment ID (or the product/term ID
  for records without one) — never random, never a counter that could
  shift between runs.
- `slugified_title` is lowercase, non-alphanumeric runs collapsed to a
  single hyphen, capped at 60 characters.
- `-{index}` (zero-padded, 2 digits) only appears for the 2nd+ image in a
  multi-image context (e.g. gallery position 2), so a single featured
  image doesn't get a redundant `-00` suffix.
- `target_format` is the source extension unchanged, except AVIF → `webp`.

Example (real, from this run): `2024/04/Razor-Blade-1.webp` →
`product-933-razor-blade.webp`. **Verified reproducible**: running the
full pipeline twice produced byte-identical `target_filename` values for
every one of 1,929 records (§ 16).

## 15. Alt-text strategy

Source data is used where it reliably exists: the 3 attachments with a
real `_wp_attachment_image_alt` value should keep it verbatim — that's
human-authored and better than anything generated. For the other 1,716
(99.8%), no reliable source description exists, so the deterministic
fallback is:

| Context | Fallback alt text |
|---|---|
| Product featured/gallery | Product title (+ position, e.g. "— image 2", for gallery items beyond the first) |
| Variant image | Product title + variant option value(s) |
| Category image | Category name (moot for this catalog — § 5 found 0 real category images) |
| Page/blog/product-body content image | Not generated here — these are prose-embedded images where a generic "{page title} image" would be low-value; flagged `REQUIRES_REVIEW` for a human to write real alt text, not defaulted |

No alt text is invented to sound more descriptive than the data supports
— a blank/generic fallback that's honestly generic is preferred over a
plausible-sounding fabrication.

## 16. Validation

Two independent verification passes, not one self-report:

1. **A real bug was found and fixed mid-phase**: WordPress stores `'0'`
   (a truthy non-empty string) for "no thumbnail set," which the first
   version of this script's `.isdigit()`/truthiness checks treated as a
   literal attachment ID 0. This produced 148 false "missing" variant
   images and misclassified all 46 category-thumbnail slots as broken.
   Fixed by explicitly excluding `'0'`/empty values before treating a
   meta value as a real attachment reference; re-run confirmed the
   corrected, much smaller (9) missing/broken count in § 10.
2. **Idempotency**: ran the full pipeline twice end-to-end. All 8 CSV
   reports and the JSON manifest were **byte-for-byte identical** across
   both runs (`diff` reported zero differences on every file).
3. **Live spot-check** (bounded, n=6, not exhaustive — downloading all
   1,719 files against production wasn't judged proportionate for a
   confidence check): 6 randomly sampled `FOUND` records' URLs fetched
   via `HEAD` request. 5 of 6 returned HTTP 200 with `Content-Length`
   exactly matching the database's stored filesize. **1 of 6 did not
   match** (`IMG_0992.jpeg`: database metadata says 771,033 bytes, the
   live file is 693,042 bytes) — the file was very likely replaced or
   re-compressed on the server after WordPress last recorded its
   metadata, a known WordPress behavior when files are swapped outside
   the Media Library UI (e.g. via FTP/hosting file manager). Flagged as
   risk #27 (§ 19) — not something this phase's DB-only method can catch
   at scale, since it requires fetching the live file to notice.

## 17. Rollback

Nothing to roll back — this phase wrote only new files (reports, manifest,
this document) and touched no WooCommerce or Shopify state. If a defect
is found in the inventory logic later, the fix is: correct
`migration/scripts/media_inventory.py`, re-run (idempotent, § 16), and
the reports/manifest are simply overwritten — there's no migrated state
anywhere yet that could be left inconsistent.

## 18. Known limitations

- Binary content-hash deduplication wasn't performed at scale (§ 4, § 9)
  — the 12 `DUPLICATE_CANDIDATE` pairs need a real hash check (or visual
  comparison) before being treated as true duplicates.
- The § 16 spot-check (n=6) is not proof every one of the 1,719
  attachments' database metadata is currently accurate — it's evidence
  the data is *mostly* reliable with at least one known exception class
  (server-side file replacement without a WordPress metadata update).
- "Decorative vs. meaningful" wasn't attempted as an automated
  classification (Step 6 of this phase's instructions listed it as a
  category) — there's no reliable signal in the database to distinguish
  a meaningful unused image from a genuinely decorative/orphaned one;
  doing so would mean inventing a field that can't be honestly populated,
  which this phase's own instructions said not to do. All 568 unused
  attachments are reported as `unused`, unclassified further, for human
  review.
- Of the 568 `unused` attachments, 8 are actually referenced by
  **non-published** (draft/trash) product or variation posts — genuinely
  unused by anything currently live, but not orphaned in the sense of
  "never attached to anything." Distinguished in the underlying data but
  not split into a separate CSV column this pass — noted here so it
  isn't lost.

## 19. Open issues / new risks

Added to `docs/RISK_REGISTER.md` as #27–29:

- **#27**: at least one attachment's live file doesn't match its
  database-recorded filesize (§ 16) — recommend a broader (not
  necessarily 100%) filesize-verification pass before Phase 9 trusts
  this inventory's `file_size` field for anything load-bearing.
- **#28**: real hex color data exists for all 332 shade/color attribute
  terms (`rey_attribute_color`) that Phase 7's theme build didn't use —
  text-pill swatches could be upgraded to real color swatches using data
  that was available all along (§ 5 bonus finding).
- **#29**: `docs/RISK_REGISTER.md` risk #25 (from the Phase 7 acceptance
  review) claimed brand logos exist in `wp_termmeta.pwb_brand_image` —
  **superseded, not just resolved**: this phase's real-data investigation
  found that source doesn't exist. Risk #25 is being corrected, not
  carried forward as-is (see § 20 of `docs/MIGRATION_PROGRESS.md`
  changelog for the full correction).

## 20. Phase 9 dependencies

- Product/variant image import can proceed directly from
  `reports/media_relationships.csv` + `migration/data/media_manifest.json`
  — no additional inventory work needed.
- Brand collections launch with no logo (§ 5) — not a Phase 9 blocker,
  but Phase 9's collection-creation step should not assume logos exist.
- 14 AVIF files need actual conversion (not just inventorying) before
  upload — a real execution step, not yet done.
- The 9 missing/broken and 12 duplicate-candidate items should get a
  human pass before Phase 9, but neither blocks Phase 9 from starting on
  the other ~1,900 clean records.
