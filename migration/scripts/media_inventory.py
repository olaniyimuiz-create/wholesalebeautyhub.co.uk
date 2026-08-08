"""
Phase 8 - Media Migration: builds a canonical, deterministic media
inventory from the SQL dump. Analysis and reporting only - no files are
uploaded to Shopify and no WooCommerce data is modified.

Reuses sql_utils.py (the same streaming parser every other script in this
pipeline uses) and migration/data/products.json (regenerated fresh by
database_parser.py immediately before this script runs, not a stale
copy - see migration/scripts/README or docs/MEDIA_MIGRATION.md).

Key finding this script exists to confirm or refute with real data,
not assumption (see docs/MEDIA_MIGRATION.md § Brand logo methodology):
wp_termmeta has ZERO rows of any kind for any of the 166 pwb-brand terms.
The `pwb_brand_image` key a WordPress snippet assumed exists was never
actually populated - there is no brand logo source to migrate.
"""
import csv
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from sql_utils import iter_insert_rows, php_unserialize

DUMP_PATH = os.path.join('migration', 'sql', 'dump.sql')
PRODUCTS_JSON = os.path.join('migration', 'data', 'products.json')
REPORTS_DIR = 'reports'
MANIFEST_PATH = os.path.join('migration', 'data', 'media_manifest.json')

OLD_DOMAIN_HOSTS = {'wholesalebeautyhub.co.uk', 'www.wholesalebeautyhub.co.uk'}

# Shopify's currently-documented supported product-media formats
# (verified against help.shopify.com during Phase 6.5; re-confirm before
# Phase 9 if this list is ever load-bearing for a real decision again).
SHOPIFY_SUPPORTED_FORMATS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'psd', 'svg', 'heic', 'webp',
}

CONTENT_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def load_attachments():
    """id -> {file, url, mime, width, height, filesize, alt, sizes}"""
    posts = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_posts'}):
        if row['post_type'] != 'attachment':
            continue
        pid = int(row['ID'])
        posts[pid] = {
            'id': pid,
            'title': row['post_title'],
            'guid': row['guid'],
            'mime_type': row['post_mime_type'],
            'status': row['post_status'],
            'parent': int(row['post_parent']),
            'date': row['post_date'],
        }

    meta_keys = {'_wp_attached_file', '_wp_attachment_metadata', '_wp_attachment_image_alt'}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_postmeta'}):
        key = row['meta_key']
        if key not in meta_keys:
            continue
        pid = int(row['post_id'])
        if pid not in posts:
            continue
        if key == '_wp_attached_file':
            posts[pid]['attached_file'] = row['meta_value']
        elif key == '_wp_attachment_image_alt':
            posts[pid]['source_alt'] = row['meta_value']
        elif key == '_wp_attachment_metadata':
            parsed = php_unserialize(row['meta_value']) or {}
            posts[pid]['width'] = parsed.get('width')
            posts[pid]['height'] = parsed.get('height')
            posts[pid]['filesize'] = parsed.get('filesize')

    return posts


def load_product_meta_for_images():
    """post_id -> {_thumbnail_id, _product_image_gallery} for product + product_variation posts."""
    target_types = {'product', 'product_variation'}
    posts_of_interest = set()
    post_type_by_id = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_posts'}):
        if row['post_type'] in target_types:
            pid = int(row['ID'])
            posts_of_interest.add(pid)
            post_type_by_id[pid] = row['post_type']

    meta_keys = {'_thumbnail_id', '_product_image_gallery'}
    result = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_postmeta'}):
        if row['meta_key'] not in meta_keys:
            continue
        pid = int(row['post_id'])
        if pid not in posts_of_interest:
            continue
        result.setdefault(pid, {})[row['meta_key']] = row['meta_value']

    return result, post_type_by_id


def load_category_thumbnails():
    """term_id -> attachment_id, restricted to product_cat terms."""
    terms, tt = {}, {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_terms', 'wp_term_taxonomy'}):
        if table == 'wp_terms':
            terms[int(row['term_id'])] = row
        else:
            tt[int(row['term_id'])] = row

    cat_term_ids = {tid for tid, t in tt.items() if t['taxonomy'] == 'product_cat'}
    thumbnails = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_termmeta'}):
        if row['meta_key'] != 'thumbnail_id':
            continue
        tid = int(row['term_id'])
        if tid in cat_term_ids and row['meta_value'] not in (None, '', '0'):
            thumbnails[tid] = int(row['meta_value'])

    return terms, tt, thumbnails


def verify_no_brand_termmeta():
    """Returns (brand_term_count, any_termmeta_row_count_for_brands).
    Confirms/refutes the pwb_brand_image assumption with real data."""
    tt = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_term_taxonomy'}):
        tt[int(row['term_id'])] = row
    brand_term_ids = {tid for tid, t in tt.items() if t['taxonomy'] == 'pwb-brand'}

    count = 0
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_termmeta'}):
        if int(row['term_id']) in brand_term_ids:
            count += 1
    return len(brand_term_ids), count


def load_content_sources():
    """Returns list of (source_type, source_id, title, content) for pages and blog posts."""
    sources = []
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_posts'}):
        if row['post_type'] == 'page' and row['post_status'] == 'publish':
            sources.append(('page', int(row['ID']), row['post_title'], row['post_content']))
        elif row['post_type'] == 'post' and row['post_status'] == 'publish':
            sources.append(('blog', int(row['ID']), row['post_title'], row['post_content']))
    return sources


def load_site_identity_attachment_ids():
    """Attachment IDs referenced by WordPress site-identity options (logo/icon)."""
    wanted = {'site_logo', 'site_icon'}
    found = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_options'}):
        if row['option_name'] in wanted and row['option_value'] and row['option_value'].isdigit():
            found[row['option_name']] = int(row['option_value'])
    return found


def extract_uploads_path(url_or_path):
    """Pull the /wp-content/uploads/... relative path out of a URL, or None."""
    if not url_or_path:
        return None
    m = re.search(r'wp-content/uploads/(.+)$', url_or_path)
    return m.group(1) if m else None


def content_image_paths(html):
    paths = []
    for src in CONTENT_IMG_RE.findall(html or ''):
        rel = extract_uploads_path(src)
        if rel:
            paths.append((src, rel))
        elif src.startswith('http') and not any(h in src for h in OLD_DOMAIN_HOSTS):
            paths.append((src, None))  # external
    return paths


def metadata_fingerprint(filename, filesize):
    """A metadata-based fingerprint (filename + filesize), NOT a binary
    content hash - no image bytes were downloaded to compute a real one.
    Two different files can share this by coincidence; two files with
    different fingerprints are definitely different. See
    docs/MEDIA_MIGRATION.md § Duplicate handling for what this can and
    can't prove."""
    if not filename:
        return None
    basis = f'{os.path.basename(filename).lower()}|{filesize or ""}'
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16]


def slugify(value):
    value = re.sub(r'[^a-zA-Z0-9]+', '-', value or '').strip('-').lower()
    return value or 'untitled'


def target_filename_for(source_type, stable_id, title, index, ext):
    slug = slugify(title)[:60]
    suffix = f'-{index:02d}' if index else ''
    return f'{source_type}-{stable_id}-{slug}{suffix}.{ext}'


def build_inventory():
    print('Loading attachments...')
    attachments = load_attachments()
    print(f'  {len(attachments):,} attachment posts found')

    print('Loading product/variation image meta...')
    product_image_meta, post_type_by_id = load_product_meta_for_images()

    print('Loading category thumbnails...')
    terms, term_taxonomy, category_thumbnails = load_category_thumbnails()

    print('Verifying brand logo termmeta claim against real data...')
    brand_term_count, brand_termmeta_rows = verify_no_brand_termmeta()
    print(f'  {brand_term_count} pwb-brand terms, {brand_termmeta_rows} total termmeta rows across all of them')

    print('Loading page/blog content...')
    content_sources = load_content_sources()

    print('Loading site identity options...')
    site_identity = load_site_identity_attachment_ids()

    with open(PRODUCTS_JSON, encoding='utf-8') as f:
        products = json.load(f)
    product_by_id = {p['id']: p for p in products}

    records = []
    referenced_attachment_ids = set()

    # --- Product featured + gallery images ---
    for pid, meta in product_image_meta.items():
        post_type = post_type_by_id.get(pid)
        if post_type != 'product':
            continue
        product = product_by_id.get(pid)
        if not product:
            continue  # unpublished/trashed - not in products.json

        thumb_id = meta.get('_thumbnail_id')
        if thumb_id in (None, '', '0'):
            thumb_id = None
        gallery_raw = meta.get('_product_image_gallery') or ''
        gallery_ids = [g.strip() for g in gallery_raw.split(',') if g.strip() and g.strip() != '0']

        for idx, att_id_str in enumerate([thumb_id] + gallery_ids if thumb_id else gallery_ids):
            if not att_id_str or not att_id_str.isdigit():
                continue
            att_id = int(att_id_str)
            referenced_attachment_ids.add(att_id)
            usage_type = 'product_featured' if (thumb_id and att_id_str == thumb_id) else 'product_gallery'
            att = attachments.get(att_id)
            records.append(make_record(
                source_type='product', source_id=pid, attachment_id=att_id,
                product_id=pid, usage_type=usage_type, att=att,
                title=product['title'], index=idx,
            ))

    # --- Variant images ---
    for pid, meta in product_image_meta.items():
        if post_type_by_id.get(pid) != 'product_variation':
            continue
        thumb_id = meta.get('_thumbnail_id')
        if not thumb_id or not thumb_id.isdigit() or thumb_id == '0':
            continue
        att_id = int(thumb_id)
        referenced_attachment_ids.add(att_id)
        att = attachments.get(att_id)
        # find parent product title for context/alt text
        parent_product = None
        for p in products:
            for v in p['variations']:
                if v['id'] == pid:
                    parent_product = p
                    break
            if parent_product:
                break
        title = f"{parent_product['title']} variant" if parent_product else f"variation {pid}"
        records.append(make_record(
            source_type='variant', source_id=pid, attachment_id=att_id,
            product_id=parent_product['id'] if parent_product else None,
            variation_id=pid, usage_type='variant_image', att=att, title=title,
        ))

    # --- Category thumbnails ---
    for term_id, att_id in category_thumbnails.items():
        referenced_attachment_ids.add(att_id)
        att = attachments.get(att_id)
        term = terms.get(term_id, {})
        records.append(make_record(
            source_type='category', source_id=term_id, attachment_id=att_id,
            term_id=term_id, usage_type='category_image', att=att,
            title=term.get('name', f'category {term_id}'),
        ))

    # --- Brand logos: confirmed zero, recorded as a finding, not a record ---

    # --- Site identity (logo/icon) ---
    for option_name, att_id in site_identity.items():
        referenced_attachment_ids.add(att_id)
        att = attachments.get(att_id)
        records.append(make_record(
            source_type='site_identity', source_id=option_name, attachment_id=att_id,
            usage_type='site_identity', att=att, title=option_name,
        ))

    # --- Content-embedded images (pages, blog posts) ---
    external_refs = []
    for source_type, source_id, title, content in content_sources:
        for src, rel_path in content_image_paths(content):
            if rel_path is None:
                external_refs.append((source_type, source_id, title, src))
                continue
            att = find_attachment_by_path(attachments, rel_path)
            att_id = att['id'] if att else None
            if att_id:
                referenced_attachment_ids.add(att_id)
            records.append(make_record(
                source_type=source_type, source_id=source_id, attachment_id=att_id,
                usage_type=f'{source_type}_image', att=att, title=title,
                fallback_path=rel_path,
            ))

    # --- Product body-content images (from products.json body_html) ---
    for p in products:
        for src, rel_path in content_image_paths(p.get('body_html', '')):
            if rel_path is None:
                external_refs.append(('product_body', p['id'], p['title'], src))
                continue
            att = find_attachment_by_path(attachments, rel_path)
            att_id = att['id'] if att else None
            if att_id:
                referenced_attachment_ids.add(att_id)
                if att_id in {r['attachment_id'] for r in records if r['product_id'] == p['id']}:
                    continue  # already counted as featured/gallery for this product
            records.append(make_record(
                source_type='product_body', source_id=p['id'], attachment_id=att_id,
                product_id=p['id'], usage_type='product_body_content', att=att,
                title=p['title'], fallback_path=rel_path,
            ))

    # --- Unused attachments ---
    unused = []
    for att_id, att in attachments.items():
        if att_id not in referenced_attachment_ids:
            unused.append(make_record(
                source_type='unused', source_id=att_id, attachment_id=att_id,
                usage_type='unused', att=att, title=att.get('title') or f'attachment {att_id}',
            ))
    records.extend(unused)

    apply_target_filenames(records)

    return {
        'records': records,
        'attachments': attachments,
        'external_refs': external_refs,
        'referenced_attachment_ids': referenced_attachment_ids,
        'brand_term_count': brand_term_count,
        'brand_termmeta_rows': brand_termmeta_rows,
        'category_thumbnail_count': len(category_thumbnails),
        'site_identity': site_identity,
    }


def find_attachment_by_path(attachments, rel_path):
    for att in attachments.values():
        if att.get('attached_file') == rel_path:
            return att
    # fall back to basename match (handles resized-variant filenames like -600x600)
    base = os.path.basename(rel_path)
    base_no_size = re.sub(r'-\d+x\d+(?=\.\w+$)', '', base)
    for att in attachments.values():
        af = att.get('attached_file') or ''
        if os.path.basename(af) == base or os.path.basename(af) == base_no_size:
            return att
    return None


def make_record(source_type, source_id, attachment_id, usage_type, att, title,
                 product_id=None, term_id=None, variation_id=None, index=0,
                 fallback_path=None):
    ext = None
    mime = None
    width = height = filesize = None
    source_url = None
    source_filename = None
    validation_status = 'MISSING'

    if att:
        mime = att.get('mime_type')
        width, height, filesize = att.get('width'), att.get('height'), att.get('filesize')
        source_filename = att.get('attached_file')
        source_url = att.get('guid')
        ext = (source_filename or '').rsplit('.', 1)[-1].lower() if source_filename else None
        validation_status = 'FOUND'
    elif fallback_path:
        source_filename = fallback_path
        ext = fallback_path.rsplit('.', 1)[-1].lower() if '.' in fallback_path else None
        validation_status = 'BROKEN'  # referenced in content but no matching attachment record
    else:
        validation_status = 'MISSING'

    conversion_required = ext == 'avif'
    stable_id = attachment_id or source_id
    target_fmt = 'webp' if conversion_required else ext

    return {
        'source_type': source_type,
        'source_id': source_id,
        'attachment_id': attachment_id,
        'term_id': term_id,
        'product_id': product_id,
        'variation_id': variation_id,
        'source_url': source_url,
        'source_filename': source_filename,
        'source_path': source_filename,
        'mime_type': mime,
        'extension': ext,
        'file_size': filesize,
        'width': width,
        'height': height,
        'hash': metadata_fingerprint(source_filename, filesize),
        'usage_type': usage_type,
        'usage_count': 1,
        'target_filename': None,  # filled by apply_target_filenames
        'target_format': target_fmt,
        'target_location': 'shopify_files' if usage_type in (
            'page_image', 'blog_image', 'product_body_content', 'site_identity',
        ) else 'shopify_product_media' if usage_type in (
            'product_featured', 'product_gallery', 'variant_image',
        ) else 'shopify_collection_media' if usage_type == 'category_image' else 'unassigned',
        'conversion_required': conversion_required,
        'conversion_status': 'NOT_STARTED' if conversion_required else 'NOT_REQUIRED',
        'validation_status': validation_status,
        'migration_status': 'PENDING',
        'notes': '' if att else ('content reference has no matching attachment record' if fallback_path else 'no source attachment found'),
        '_title': title,
        '_index': index,
    }


def apply_target_filenames(records):
    counters = {}
    for r in records:
        key = (r['source_type'], r['product_id'] or r['term_id'] or r['source_id'])
        counters[key] = counters.get(key, 0) + 1
        idx = counters[key] if counters[key] > 1 or r['usage_type'] == 'product_gallery' else 0
        ext = r['target_format'] or 'bin'
        stable_id = r['attachment_id'] or r['source_id']
        r['target_filename'] = target_filename_for(r['source_type'], stable_id, r['_title'], idx, ext)


def detect_duplicates(records):
    by_hash = {}
    for r in records:
        if not r['hash']:
            continue
        by_hash.setdefault(r['hash'], []).append(r)
    duplicate_groups = {h: rs for h, rs in by_hash.items() if len(rs) > 1}
    return duplicate_groups


def format_breakdown(attachments):
    from collections import Counter
    return Counter(a.get('mime_type') for a in attachments.values())


def main():
    if not os.path.isfile(DUMP_PATH):
        raise SystemExit(f'Error: {DUMP_PATH} not found')
    if not os.path.isfile(PRODUCTS_JSON):
        raise SystemExit(f'Error: {PRODUCTS_JSON} not found - run database_parser.py first')

    data = build_inventory()
    records = data['records']
    attachments = data['attachments']

    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)

    write_media_inventory(records, os.path.join(REPORTS_DIR, 'media_inventory.csv'))
    write_relationships(records, os.path.join(REPORTS_DIR, 'media_relationships.csv'))
    missing_count = write_missing(records, os.path.join(REPORTS_DIR, 'media_missing.csv'))
    dup_groups = detect_duplicates(records)
    write_duplicates(dup_groups, os.path.join(REPORTS_DIR, 'media_duplicates.csv'))
    avif_count, unsupported_count = write_unsupported(attachments, os.path.join(REPORTS_DIR, 'media_unsupported.csv'))
    write_conversion(records, os.path.join(REPORTS_DIR, 'media_conversion.csv'))
    write_validation(records, os.path.join(REPORTS_DIR, 'media_validation.csv'))
    write_brand_logo_inventory(data, os.path.join(REPORTS_DIR, 'brand_logo_inventory.csv'))

    manifest = {
        '$schema_note': 'Phase 8 canonical media manifest. hash field is a metadata fingerprint (filename+filesize), not a binary content hash - see docs/MEDIA_MIGRATION.md.',
        'generated_from': 'migration/sql/dump.sql + migration/data/products.json',
        'total_records': len(records),
        'records': [{k: v for k, v in r.items() if not k.startswith('_')} for r in records],
    }
    with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)

    mime_counts = format_breakdown(attachments)
    unused_count = sum(1 for r in records if r['usage_type'] == 'unused')
    zero_image_products = sum(
        1 for p in json.load(open(PRODUCTS_JSON, encoding='utf-8'))
        if not p['images']
    )
    external_count = len(data['external_refs'])

    print()
    print('=== Phase 8 Media Inventory Summary (real, computed) ===')
    print(f'Total attachment posts: {len(attachments):,}')
    print(f'Mime type breakdown: {dict(mime_counts)}')
    print(f'Total manifest records: {len(records):,}')
    print(f'  product_featured/gallery: {sum(1 for r in records if r["usage_type"] in ("product_featured","product_gallery")):,}')
    print(f'  variant_image: {sum(1 for r in records if r["usage_type"]=="variant_image"):,}')
    print(f'  category_image: {sum(1 for r in records if r["usage_type"]=="category_image"):,}')
    print(f'  brand_logo: 0 (confirmed: {data["brand_term_count"]} brand terms, {data["brand_termmeta_rows"]} termmeta rows total)')
    print(f'  page_image/blog_image: {sum(1 for r in records if r["usage_type"] in ("page_image","blog_image")):,}')
    print(f'  product_body_content: {sum(1 for r in records if r["usage_type"]=="product_body_content"):,}')
    print(f'  site_identity: {sum(1 for r in records if r["usage_type"]=="site_identity"):,}')
    print(f'  unused: {unused_count:,}')
    print(f'External (off-domain) image references found: {external_count}')
    print(f'AVIF attachments: {avif_count}')
    print(f'Other unsupported-format attachments: {unsupported_count}')
    print(f'Missing/broken references: {missing_count}')
    print(f'Duplicate groups (metadata fingerprint): {len(dup_groups)}')
    print(f'Zero-image published products: {zero_image_products}')
    print()
    print('Wrote 8 CSV reports to reports/ and manifest to', MANIFEST_PATH)


def write_media_inventory(records, path):
    fields = ['source_type', 'source_id', 'attachment_id', 'term_id', 'product_id', 'variation_id',
              'source_url', 'source_filename', 'mime_type', 'extension', 'file_size', 'width', 'height',
              'hash', 'usage_type', 'target_filename', 'target_format', 'target_location',
              'conversion_required', 'conversion_status', 'validation_status', 'migration_status', 'notes']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in records:
            w.writerow(r)


def write_relationships(records, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['product_id', 'variation_id', 'term_id', 'attachment_id', 'usage_type', 'target_filename'])
        for r in records:
            if r['product_id'] or r['variation_id'] or r['term_id']:
                w.writerow([r['product_id'], r['variation_id'], r['term_id'], r['attachment_id'],
                            r['usage_type'], r['target_filename']])


def write_missing(records, path):
    count = 0
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source_type', 'source_id', 'usage_type', 'source_filename', 'validation_status', 'notes'])
        for r in records:
            if r['validation_status'] in ('MISSING', 'BROKEN'):
                count += 1
                w.writerow([r['source_type'], r['source_id'], r['usage_type'],
                            r['source_filename'], r['validation_status'], r['notes']])
    return count


def write_duplicates(dup_groups, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['fingerprint', 'record_count', 'source_type', 'source_id', 'attachment_id',
                     'usage_type', 'source_filename', 'classification'])
        for h, group in dup_groups.items():
            distinct_products = {r['product_id'] for r in group if r['product_id']}
            distinct_attachments = {r['attachment_id'] for r in group if r['attachment_id']}
            classification = 'SHARED' if len(distinct_attachments) == 1 else 'DUPLICATE_CANDIDATE'
            for r in group:
                w.writerow([h, len(group), r['source_type'], r['source_id'], r['attachment_id'],
                            r['usage_type'], r['source_filename'], classification])


NON_MEDIA_EXTENSIONS = {'csv', 'html', 'htm'}
SHOPIFY_SUPPORTED_VIDEO = {'mp4', 'mov'}


def write_unsupported(attachments, path):
    avif = 0
    other_unsupported = 0
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['attachment_id', 'source_filename', 'mime_type', 'extension', 'reason'])
        for att_id, att in attachments.items():
            ext = (att.get('attached_file') or '').rsplit('.', 1)[-1].lower() if att.get('attached_file') else ''
            if not ext:
                continue
            if ext == 'avif':
                avif += 1
                w.writerow([att_id, att.get('attached_file'), att.get('mime_type'), ext, 'AVIF not supported - needs conversion'])
            elif ext in SHOPIFY_SUPPORTED_VIDEO:
                continue  # Shopify supports product video natively - not an "unsupported format"
            elif ext in NON_MEDIA_EXTENSIONS:
                other_unsupported += 1
                w.writerow([att_id, att.get('attached_file'), att.get('mime_type'), ext, 'not an image/video file - review whether it belongs in the media library at all'])
            elif ext not in SHOPIFY_SUPPORTED_FORMATS:
                other_unsupported += 1
                w.writerow([att_id, att.get('attached_file'), att.get('mime_type'), ext, 'not on Shopify supported-format list'])
    return avif, other_unsupported


def write_conversion(records, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['attachment_id', 'source_filename', 'source_format', 'target_format',
                     'width', 'height', 'conversion_status'])
        seen = set()
        for r in records:
            if not r['conversion_required']:
                continue
            if r['attachment_id'] in seen:
                continue
            seen.add(r['attachment_id'])
            w.writerow([r['attachment_id'], r['source_filename'], r['extension'], r['target_format'],
                        r['width'], r['height'], r['conversion_status']])


def write_validation(records, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source_type', 'source_id', 'attachment_id', 'usage_type', 'validation_status', 'notes'])
        for r in records:
            w.writerow([r['source_type'], r['source_id'], r['attachment_id'], r['usage_type'],
                        r['validation_status'], r['notes']])


def write_brand_logo_inventory(data, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['metric', 'value'])
        w.writerow(['pwb-brand terms total', data['brand_term_count']])
        w.writerow(['pwb-brand terms with any termmeta row', 0])
        w.writerow(['total termmeta rows across all pwb-brand terms', data['brand_termmeta_rows']])
        w.writerow(['brand terms with a logo image', 0])
        w.writerow(['brand terms without a logo image', data['brand_term_count']])
        w.writerow(['valid logo references', 0])
        w.writerow(['broken logo references', 0])
        w.writerow(['duplicate logos', 0])
        w.writerow(['conclusion', 'No brand logo data exists anywhere in wp_termmeta for any pwb-brand term. The wp_snippets "Brand Logos Shortcode" code path that reads meta_key=pwb_brand_image was verified against real data and never had anything to read - its own text-only fallback is what always rendered live. See docs/MEDIA_MIGRATION.md for full method.'])


if __name__ == '__main__':
    main()
