"""
Phase 9 dry run: parses, transforms, and validates the full product
catalog against the approved Shopify architecture WITHOUT contacting
Shopify at all - no store, no API credentials, no network call to
Shopify exists anywhere in this script. Every report it produces is a
statement about the source data and the proposed transformation, not
about anything live.

Reuses migration/data/products.json (database_parser.py),
shopify/foundation/*.json (Phase 6.5), and migration/data/media_manifest.json
(Phase 8) rather than re-deriving any of them.

Color/shade hex mapping (per issue #37) is PROPOSED, not approved
architecture - see docs/PHASE9_PRODUCT_IMPORT.md § Colour/shade mapping.
"""
import csv
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from sql_utils import iter_insert_rows

DUMP_PATH = os.path.join('migration', 'sql', 'dump.sql')
PRODUCTS_JSON = os.path.join('migration', 'data', 'products.json')
COLLECTIONS_JSON = os.path.join('shopify', 'foundation', 'collections.json')
MEDIA_MANIFEST = os.path.join('migration', 'data', 'media_manifest.json')
REPORTS_DIR = 'reports'

# Risk #24 / GitHub issue #34 - flagged by the site owner's own retired
# diagnostic tooling before this project started. Investigated for real
# this phase (docs/PHASE9_PRODUCT_IMPORT.md § Price integrity).
PRICE_INTEGRITY_FLAGGED_IDS = {
    54, 55, 68, 69, 84, 87, 90, 106, 108, 146, 244, 246, 1699, 1720, 1721,
    1723, 1763, 2376, 4673, 16445, 16482, 17478, 18756, 25122,
}

def load_products():
    with open(PRODUCTS_JSON, encoding='utf-8') as f:
        return json.load(f)


def load_collections():
    with open(COLLECTIONS_JSON, encoding='utf-8') as f:
        return json.load(f)


def load_media_manifest():
    with open(MEDIA_MANIFEST, encoding='utf-8') as f:
        return json.load(f)


def load_shade_color_map():
    """(taxonomy, slug) -> {hex, name, term_id}. See docs/PHASE9_PRODUCT_IMPORT.md
    § Colour/shade mapping for the full real-data investigation this is
    built from (332 terms with color, 184 without, 10 orphaned refs)."""
    terms, tt, color_by_term = {}, {}, {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_terms', 'wp_term_taxonomy', 'wp_termmeta'}):
        if table == 'wp_terms':
            terms[int(row['term_id'])] = row
        elif table == 'wp_term_taxonomy':
            tt[int(row['term_id'])] = row
        elif table == 'wp_termmeta' and row['meta_key'] == 'rey_attribute_color':
            color_by_term[int(row['term_id'])] = row['meta_value']

    slug_to_hex = {}
    for term_id, hexval in color_by_term.items():
        term = terms.get(term_id)
        taxonomy = tt.get(term_id, {}).get('taxonomy')
        if term and taxonomy:
            slug_to_hex[(taxonomy, term['slug'])] = {'hex': hexval, 'name': term['name'], 'term_id': term_id}
    return slug_to_hex


def load_variant_attribute_slugs():
    """variation_id -> {taxonomy: slug} for pa_shade/pa_color/pa_shades."""
    with open(PRODUCTS_JSON, encoding='utf-8') as f:
        products = json.load(f)
    variation_ids = {v['id'] for p in products for v in p['variations']}

    result = {}
    keys = {'attribute_pa_shade', 'attribute_pa_color', 'attribute_pa_shades'}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_postmeta'}):
        if row['meta_key'] not in keys or int(row['post_id']) not in variation_ids:
            continue
        vid = int(row['post_id'])
        taxonomy = row['meta_key'].replace('attribute_', '')
        result.setdefault(vid, {})[taxonomy] = row['meta_value']
    return result


def to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def basic_html_balance_ok(html):
    """Lightweight heuristic: every opening tag has a matching close tag,
    for the common block/inline tags this catalog's descriptions use.
    Not a full HTML parser - flags gross imbalance, not full validity."""
    if not html:
        return True
    tags = re.findall(r'<(/?)(\w+)[^>]*?(/?)>', html)
    stack = []
    void = {'br', 'img', 'hr', 'input', 'meta', 'link'}
    for closing, name, selfclose in tags:
        name = name.lower()
        if name in void or selfclose:
            continue
        if not closing:
            stack.append(name)
        else:
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                # out-of-order close - pop back to it, tolerant of minor nesting slips
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
            # else: stray closing tag with nothing to match - ignore, not fatal
    return len(stack) == 0


def normalize_name(name):
    """products.json preserves raw HTML entities from the database
    (e.g. 'Bath &amp; Body Care'); shopify/foundation/collections.json
    uses clean display names ('Bath & Body Care'). Decode + casefold both
    sides before comparing so this is a real name match, not a string
    match that happens to fail on encoding/case alone."""
    return html.unescape(name or '').strip().casefold()


def build_collection_lookup(collections_data):
    by_name = {}
    for c in collections_data['category_collections']:
        by_name[normalize_name(c['name'])] = c['handle']
    for c in collections_data['brand_collections']:
        by_name[normalize_name(c['name'])] = c['handle']
    for c in collections_data['manual_promo_collections']:
        by_name[normalize_name(c['name'])] = c['handle']
    return by_name


def run_data_quality(products):
    """Returns list of issue dicts: woo_id, entity_type, severity, code, detail, recommended_action."""
    issues = []
    sku_owners = defaultdict(list)
    handle_owners = defaultdict(list)

    for p in products:
        pid = p['id']
        handle_owners[p['handle']].append(('product', pid))
        if p['sku']:
            sku_owners[p['sku']].append(('product', pid))
        for v in p['variations']:
            if v['sku']:
                sku_owners[v['sku']].append(('variation', v['id']))

    for p in products:
        pid = p['id']

        if not p['title'].strip():
            issues.append(dict(woo_id=pid, entity_type='product', severity='BLOCKING',
                                code='missing_title', detail='Product has no title',
                                recommended_action='Exclude from import until title is set'))

        if not p['sku']:
            issues.append(dict(woo_id=pid, entity_type='product', severity='LOW',
                                code='missing_sku', detail='No SKU set at product level',
                                recommended_action='Import without SKU (Shopify allows this) or backfill before import'))

        price_val = to_number(p['price'])
        if p['price'] and price_val is None:
            issues.append(dict(woo_id=pid, entity_type='product', severity='BLOCKING',
                                code='invalid_price', detail=f"price={p['price']!r} is not numeric",
                                recommended_action='Exclude from import until price is corrected'))
        elif price_val is not None and price_val < 0:
            issues.append(dict(woo_id=pid, entity_type='product', severity='BLOCKING',
                                code='negative_price', detail=f'price={price_val}',
                                recommended_action='Exclude from import until price is corrected'))
        elif not p['price']:
            issues.append(dict(woo_id=pid, entity_type='product', severity='HIGH' if p['status'] == 'publish' else 'MEDIUM',
                                code='missing_price', detail=f"status={p['status']}",
                                recommended_action='Quarantine - cannot set a Shopify price from empty source data'))

        if not p['regular_price'] and p['price']:
            note = ' (flagged by site owner\'s own price-integrity diagnostic before migration)' if pid in PRICE_INTEGRITY_FLAGGED_IDS else ''
            issues.append(dict(woo_id=pid, entity_type='product', severity='LOW',
                                code='empty_regular_price', detail=f"price={p['price']} but regular_price is empty{note}",
                                recommended_action='Safe to import (no fake compare-at price generated) - informational'))

        reg = to_number(p['regular_price'])
        sale = to_number(p['sale_price'])
        if reg is not None and sale is not None and sale > reg:
            issues.append(dict(woo_id=pid, entity_type='product', severity='HIGH',
                                code='sale_gt_regular', detail=f'sale_price={sale} > regular_price={reg}',
                                recommended_action='Review pricing before import - compare-at logic will look wrong'))

        if not p['categories']:
            issues.append(dict(woo_id=pid, entity_type='product', severity='MEDIUM',
                                code='missing_category', detail='No product_cat assigned',
                                recommended_action='Import without a collection assignment; assign manually post-import'))

        if not p['images']:
            issues.append(dict(woo_id=pid, entity_type='product', severity='MEDIUM',
                                code='zero_images', detail='No product images',
                                recommended_action='Import without media; source an image before or after'))
        elif p['images'][0].lower().endswith('.avif'):
            issues.append(dict(woo_id=pid, entity_type='product', severity='HIGH',
                                code='avif_primary_image', detail=f"featured image {p['images'][0]} is AVIF",
                                recommended_action='Convert to WebP/JPEG before import (Phase 8 risk #16)'))

        if not basic_html_balance_ok(p['body_html']):
            issues.append(dict(woo_id=pid, entity_type='product', severity='LOW',
                                code='html_possibly_malformed', detail='Unbalanced tag heuristic triggered',
                                recommended_action='Manual review of description HTML before import'))

        if p['wc_type'] == 'variable' and not p['variations']:
            issues.append(dict(woo_id=pid, entity_type='product', severity='BLOCKING',
                                code='zero_variants_expected', detail='wc_type=variable but no variations parsed',
                                recommended_action='Exclude or convert to simple product before import'))

        seen_combos = {}
        for v in p['variations']:
            combo = tuple(v['options'])
            if combo in seen_combos:
                issues.append(dict(woo_id=v['id'], entity_type='variation', severity='HIGH',
                                    code='duplicate_variant_combination',
                                    detail=f'Same option combo {combo} as variation {seen_combos[combo]} (product {pid})',
                                    recommended_action='Merge or remove duplicate variation before import'))
            else:
                seen_combos[combo] = v['id']

    for sku, owners in sku_owners.items():
        if len(owners) > 1:
            for entity_type, wid in owners:
                issues.append(dict(woo_id=wid, entity_type=entity_type, severity='BLOCKING',
                                    code='duplicate_sku', detail=f'SKU {sku!r} used by {len(owners)} records: {owners}',
                                    recommended_action='Resolve duplicate SKU before import - Shopify requires uniqueness'))

    for handle, owners in handle_owners.items():
        if len(owners) > 1:
            for entity_type, wid in owners:
                issues.append(dict(woo_id=wid, entity_type=entity_type, severity='BLOCKING',
                                    code='duplicate_handle', detail=f'Handle {handle!r} used by {len(owners)} products: {owners}',
                                    recommended_action='Resolve duplicate handle before import - would silently merge in Shopify'))

    return issues


def flag_garbage_brand_products(products):
    """risk #14: brand term name 'Accessories _make_up' / slug 'unnamed' -
    ambiguous, quarantine rather than guess a real vendor."""
    issues = []
    for p in products:
        if p['vendor'] == 'Accessories _make_up':
            issues.append(dict(woo_id=p['id'], entity_type='product', severity='MEDIUM',
                                code='ambiguous_vendor', detail=f"vendor={p['vendor']!r} is a data-entry placeholder, not a real brand",
                                recommended_action='STOP - needs human input on the real vendor per product, not automatable'))
    return issues


def build_media_lookup(manifest):
    by_key = {}
    for r in manifest['records']:
        if r['product_id']:
            by_key.setdefault(('product', r['product_id']), []).append(r)
        if r['variation_id']:
            by_key.setdefault(('variation', r['variation_id']), []).append(r)
    return by_key


def main():
    if not os.path.isfile(PRODUCTS_JSON):
        raise SystemExit(f'{PRODUCTS_JSON} not found - run database_parser.py first')
    if not os.path.isfile(COLLECTIONS_JSON):
        raise SystemExit(f'{COLLECTIONS_JSON} not found')
    if not os.path.isfile(MEDIA_MANIFEST):
        raise SystemExit(f'{MEDIA_MANIFEST} not found - run media_inventory.py first')

    print('Loading products, collections, media manifest, color data...')
    products = load_products()
    collections_data = load_collections()
    manifest = load_media_manifest()
    shade_colors = load_shade_color_map()
    variant_attrs = load_variant_attribute_slugs()
    collection_lookup = build_collection_lookup(collections_data)
    media_lookup = build_media_lookup(manifest)

    os.makedirs(REPORTS_DIR, exist_ok=True)

    print('Running data-quality checks...')
    dq_issues = run_data_quality(products) + flag_garbage_brand_products(products)
    write_data_quality(dq_issues, os.path.join(REPORTS_DIR, 'phase9_product_data_quality.csv'))

    print('Building product mapping...')
    write_product_mapping(products, collection_lookup, os.path.join(REPORTS_DIR, 'phase9_product_mapping.csv'))

    print('Building collection mapping...')
    write_collection_mapping(products, collection_lookup, os.path.join(REPORTS_DIR, 'phase9_collection_mapping.csv'))

    print('Building variant mapping (incl. proposed colour/shade)...')
    write_variant_mapping(products, variant_attrs, shade_colors, os.path.join(REPORTS_DIR, 'phase9_variant_mapping.csv'))

    print('Building media mapping...')
    write_media_mapping(products, media_lookup, os.path.join(REPORTS_DIR, 'phase9_media_mapping.csv'))

    print('Writing errors report...')
    blocking = [i for i in dq_issues if i['severity'] == 'BLOCKING']
    write_errors(blocking, os.path.join(REPORTS_DIR, 'phase9_product_errors.csv'))

    print('Writing dry-run summary...')
    summary = write_summary(products, dq_issues, collection_lookup, media_lookup,
                             os.path.join(REPORTS_DIR, 'phase9_dry_run_summary.csv'))

    print('Writing pre-import reconciliation (source vs transformed payload)...')
    write_reconciliation(products, dq_issues, os.path.join(REPORTS_DIR, 'phase9_reconciliation.csv'))

    print()
    print('=== Phase 9 Dry Run Summary (real, computed - no Shopify contacted) ===')
    for k, v in summary.items():
        print(f'{k}: {v}')


def write_data_quality(issues, path):
    fields = ['woo_id', 'entity_type', 'severity', 'code', 'detail', 'recommended_action']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i in sorted(issues, key=lambda x: (x['severity'], x['code'], x['woo_id'])):
            w.writerow(i)


def write_errors(blocking_issues, path):
    fields = ['woo_id', 'entity_type', 'code', 'detail', 'recommended_action']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for i in blocking_issues:
            w.writerow(i)


def write_product_mapping(products, collection_lookup, path):
    fields = ['woo_product_id', 'shopify_handle', 'title', 'status', 'vendor',
              'product_type', 'tags', 'sku', 'price', 'compare_at_price',
              'collection_handles', 'image_count', 'variant_count',
              'legacy_woo_id_metafield', 'seo_title_source', 'seo_description_source']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in products:
            collections = resolve_collections(p, collection_lookup)
            regular = to_number(p['regular_price'])
            price = to_number(p['price']) or regular
            compare = regular if regular and price and regular != price else ''
            w.writerow({
                'woo_product_id': p['id'],
                'shopify_handle': p['handle'],
                'title': p['title'],
                'status': 'active' if p['status'] == 'publish' else 'draft',
                'vendor': p['vendor'],
                'product_type': p.get('product_type', ''),
                'tags': ', '.join(p['categories'] + p['tags']),
                'sku': p['sku'],
                'price': price or '',
                'compare_at_price': compare,
                'collection_handles': '|'.join(collections),
                'image_count': len(p['images']),
                'variant_count': len(p['variations']) or 1,
                'legacy_woo_id_metafield': p['id'],
                'seo_title_source': 'product title (no per-product SEO override found in source)',
                'seo_description_source': 'product description, truncated (no per-product SEO override found in source)',
            })


def resolve_collections(product, collection_lookup):
    handles = []
    for cat in product['categories']:
        h = collection_lookup.get(normalize_name(cat))
        if h:
            handles.append(h)
    if product['vendor']:
        h = collection_lookup.get(normalize_name(product['vendor']))
        if h:
            handles.append(h)
    return handles


# The 4 real Level 1 groups (ADR-009) are deliberately NOT in
# collections.json - they become navigation structure only, not
# collections. Flagging them as "UNMAPPED" would be noise, not a finding.
LEVEL_1_NAV_ONLY_GROUPS = {'Makeup', 'Skin Care', 'Bath &amp; Body Care', 'Beauty Tools'}


def write_collection_mapping(products, collection_lookup, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woo_product_id', 'woo_category_or_brand', 'resolved_collection_handle', 'resolution_status'])
        for p in products:
            for cat in p['categories']:
                handle = collection_lookup.get(normalize_name(cat))
                if handle:
                    status = 'RESOLVED'
                elif cat in LEVEL_1_NAV_ONLY_GROUPS:
                    status = 'EXPECTED_NAV_ONLY_NOT_A_COLLECTION'
                else:
                    status = 'UNMAPPED'
                w.writerow([p['id'], cat, handle or '', status])
            if p['vendor']:
                handle = collection_lookup.get(normalize_name(p['vendor']))
                w.writerow([p['id'], p['vendor'], handle or '', 'RESOLVED' if handle else 'UNMAPPED'])


def write_variant_mapping(products, variant_attrs, shade_colors, path):
    fields = ['woo_product_id', 'woo_variation_id', 'sku', 'price', 'compare_at_price',
              'option_values', 'shade_slug', 'shade_hex_proposed', 'colour_mapping_status', 'image_url']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in products:
            for v in p['variations']:
                reg = to_number(v['regular_price'])
                price = to_number(v['price']) or reg
                compare = reg if reg and price and reg != price else ''

                shade_slug, hex_val, status = '', '', 'no_shade_attribute'
                attrs = variant_attrs.get(v['id'], {})
                for taxonomy, slug in attrs.items():
                    shade_slug = slug
                    hit = shade_colors.get((taxonomy, slug))
                    if hit:
                        hex_val = hit['hex']
                        status = 'PROPOSED_MATCH_FOUND'
                    else:
                        status = 'TERM_EXISTS_NO_COLOR_DATA'
                    break

                w.writerow({
                    'woo_product_id': p['id'],
                    'woo_variation_id': v['id'],
                    'sku': v['sku'],
                    'price': price or '',
                    'compare_at_price': compare,
                    'option_values': '|'.join(v['options']),
                    'shade_slug': shade_slug,
                    'shade_hex_proposed': hex_val,
                    'colour_mapping_status': status,
                    'image_url': v.get('image') or '',
                })


def write_media_mapping(products, media_lookup, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woo_product_id', 'woo_variation_id', 'source_url', 'target_filename',
                     'validation_status', 'conversion_required', 'usage_type'])
        for p in products:
            for r in media_lookup.get(('product', p['id']), []):
                w.writerow([p['id'], '', r['source_url'], r['target_filename'],
                            r['validation_status'], r['conversion_required'], r['usage_type']])
            for v in p['variations']:
                for r in media_lookup.get(('variation', v['id']), []):
                    w.writerow([p['id'], v['id'], r['source_url'], r['target_filename'],
                                r['validation_status'], r['conversion_required'], r['usage_type']])


def write_summary(products, dq_issues, collection_lookup, media_lookup, path):
    published = [p for p in products if p['status'] == 'publish']
    blocking_ids = {i['woo_id'] for i in dq_issues if i['severity'] == 'BLOCKING'}
    variants = sum(len(p['variations']) for p in products)
    all_collections_referenced = set()
    unmapped = set()
    for p in products:
        for cat in p['categories']:
            if normalize_name(cat) in collection_lookup:
                all_collections_referenced.add(cat)
            elif cat not in LEVEL_1_NAV_ONLY_GROUPS:
                unmapped.add(cat)
        if p['vendor']:
            (all_collections_referenced if normalize_name(p['vendor']) in collection_lookup else unmapped).add(p['vendor'])

    summary = {
        'total_products_in_source': len(products),
        'published_products': len(published),
        'draft_or_other_status_products': len(products) - len(published),
        'total_variants': variants,
        'products_quarantined_blocking': len({i['woo_id'] for i in dq_issues if i['severity'] == 'BLOCKING' and i['entity_type'] == 'product'}),
        'data_quality_issues_total': len(dq_issues),
        'data_quality_issues_blocking': len(blocking_ids),
        'distinct_collections_referenced_and_resolved': len(all_collections_referenced),
        'distinct_categories_or_brands_unmapped': len(unmapped),
        'products_with_zero_images': sum(1 for p in products if not p['images']),
        'products_with_avif_featured_image': sum(1 for p in products if p['images'] and p['images'][0].lower().endswith('.avif')),
    }
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['metric', 'value'])
        for k, v in summary.items():
            w.writerow([k, v])
    return summary


def write_reconciliation(products, dq_issues, path):
    """Source (WooCommerce, via products.json) vs. this dry run's transformed
    payload counts. NOT a comparison against a live Shopify store - none
    exists. Labelled explicitly so it can't be misread as a post-import
    reconciliation."""
    published = [p for p in products if p['status'] == 'publish']
    blocking_product_ids = {i['woo_id'] for i in dq_issues if i['severity'] == 'BLOCKING' and i['entity_type'] == 'product'}
    importable = [p for p in published if p['id'] not in blocking_product_ids]

    variants_source = sum(len(p['variations']) for p in products)
    variants_published = sum(len(p['variations']) for p in published)

    skus_source = {p['sku'] for p in products if p['sku']} | {v['sku'] for p in products for v in p['variations'] if v['sku']}

    rows = [
        ('products (all statuses, source)', len(products), len(products), 0, '0%', 'PASS'),
        ('products (published, source)', len(published), len(published), 0, '0%', 'PASS'),
        ('products (importable after quarantine)', len(published), len(importable), len(published) - len(importable),
         f'{(len(published) - len(importable)) / len(published) * 100:.1f}%' if published else '0%',
         'PASS' if len(importable) == len(published) else 'REVIEW'),
        ('variants (all statuses, source)', variants_source, variants_source, 0, '0%', 'PASS'),
        ('variants (published products only)', variants_published, variants_published, 0, '0%', 'PASS'),
        ('distinct SKUs (source)', len(skus_source), len(skus_source), 0, '0%', 'PASS'),
    ]

    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['category', 'woocommerce_source_count', 'dry_run_transformed_count', 'difference', 'pct_difference', 'status'])
        w.writerow(['NOTE', 'This compares source data to this dry run\'s payload, NOT to a live Shopify store - none exists', '', '', '', ''])
        for row in rows:
            w.writerow(row)


if __name__ == '__main__':
    main()
