"""
Phase 9.7 - final import manifest: one row per product in the full
611-product catalog, classified ALREADY_IMPORTED / IMPORT / QUARANTINE /
EXCLUDE.

Reuses phase9_dry_run.py's existing data-quality/quarantine rules and
collection resolution, and phase9_test_import.py's tag-building logic,
rather than inventing new classification logic - this is a reporting
step, not a new decision layer.

Read-only against Shopify (only to know which legacy IDs already exist,
so they're not double-counted as "remaining") - zero writes.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_dry_run import (
    load_products, load_collections, run_data_quality,
    flag_garbage_brand_products, flag_incomplete_variations, flag_missing_price,
    resolve_collections, build_collection_lookup, to_number,
    partition_variations, partition_by_price,
)
from phase9_test_import import build_tags
from phase9_preflight import get_config, graphql_request

# incomplete_variation/partial_missing_price are informational only
# (Option A, Phase 9.7 Step 5/pricing-safety recovery): the broken
# variation/unpriced variation is skipped, the parent product still
# imports - it does NOT quarantine the product. no_source_price DOES
# quarantine (a simple product or fully-unpriced variable product has
# nothing sellable to create at all).
QUARANTINE_CODES = ('ambiguous_vendor', 'no_source_price')

REPORTS_DIR = 'reports'
OUT_PATH = os.path.join(REPORTS_DIR, 'phase9_final_import_manifest.csv')


def fetch_already_imported_legacy_ids(domain, token, api_version):
    """Every product currently in the store, keyed by its
    custom.legacy_woo_id metafield - the same lookup mechanism
    phase9_test_import.py's idempotency already relies on."""
    ids = set()
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        query = ('{ products(first: 50%s) { pageInfo { hasNextPage endCursor } '
                  'edges { node { metafield(namespace: "custom", key: "legacy_woo_id") { value } } } } }') % after
        data = graphql_request(domain, token, api_version, query)
        for edge in data['data']['products']['edges']:
            mf = edge['node']['metafield']
            if mf:
                ids.add(int(mf['value']))
        page = data['data']['products']['pageInfo']
        if not page['hasNextPage']:
            return ids
        cursor = page['endCursor']


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - cannot confirm which products are already imported without live credentials.')
        return 2

    already_imported = fetch_already_imported_legacy_ids(domain, token, api_version)
    print(f'{len(already_imported)} product(s) already in the store (by legacy_woo_id): {sorted(already_imported)}')

    products = load_products()
    collection_lookup = build_collection_lookup(load_collections())
    dq_issues = (run_data_quality(products) + flag_garbage_brand_products(products)
                 + flag_incomplete_variations(products) + flag_missing_price(products))

    blocking_ids = {i['woo_id'] for i in dq_issues if i['severity'] == 'BLOCKING' and i['entity_type'] == 'product'}
    quarantine_ids = {i['woo_id'] for i in dq_issues if i.get('code') in QUARANTINE_CODES}

    fields = ['woo_product_id', 'shopify_legacy_id', 'title', 'product_type', 'vendor', 'sku',
              'price', 'compare_at_price', 'inventory_manage_stock', 'inventory_stock_quantity',
              'status', 'collections', 'tags', 'variant_count', 'image_count',
              'variation_count_source', 'variation_count_valid', 'variation_count_skipped',
              'skipped_variation_ids', 'variation_count_missing_price', 'missing_price_variation_ids',
              'skip_reason', 'quarantine_status', 'import_eligibility']

    counts = {'ALREADY_IMPORTED': 0, 'IMPORT': 0, 'QUARANTINE': 0, 'EXCLUDE': 0}

    with open(OUT_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in products:
            reg = to_number(p['regular_price'])
            price = to_number(p['price']) or reg
            compare = reg if reg and price and reg != price else ''
            collections = resolve_collections(p, collection_lookup)

            if p['wc_type'] == 'variable' and p['variations']:
                option_valid, skipped_variations = partition_variations(p['variations'])
                priced_variations, missing_price_variations = partition_by_price(option_valid)
            else:
                option_valid, skipped_variations = [], []
                priced_variations, missing_price_variations = [], []

            if p['id'] in already_imported:
                eligibility = 'ALREADY_IMPORTED'
                quarantine_status = ''
            elif p['id'] in blocking_ids:
                eligibility = 'EXCLUDE'
                quarantine_status = '; '.join(i['detail'] for i in dq_issues if i['woo_id'] == p['id'] and i['severity'] == 'BLOCKING')
            elif p['id'] in quarantine_ids:
                eligibility = 'QUARANTINE'
                quarantine_status = '; '.join(i['detail'] for i in dq_issues if i['woo_id'] == p['id'] and i.get('code') in QUARANTINE_CODES)
            else:
                eligibility = 'IMPORT'
                quarantine_status = ''

            counts[eligibility] += 1

            w.writerow({
                'woo_product_id': p['id'],
                'shopify_legacy_id': p['id'],
                'title': p['title'],
                'product_type': p.get('product_type', ''),
                'vendor': p['vendor'],
                'sku': p['sku'],
                'price': price or '',
                'compare_at_price': compare,
                'inventory_manage_stock': p['manage_stock'],
                'inventory_stock_quantity': p['stock_quantity'] or '',
                'status': 'ACTIVE' if p['status'] == 'publish' else 'DRAFT',
                'collections': '|'.join(collections),
                'tags': '|'.join(build_tags(p)),
                'variant_count': (len(priced_variations) or 1) if (p['wc_type'] == 'variable' and p['variations']) else 1,
                'image_count': len(p['images']),
                'variation_count_source': len(p['variations']) if (p['wc_type'] == 'variable' and p['variations']) else '',
                'variation_count_valid': len(option_valid) if (p['wc_type'] == 'variable' and p['variations']) else '',
                'variation_count_skipped': len(skipped_variations),
                'skipped_variation_ids': '|'.join(str(v['id']) for v in skipped_variations),
                'variation_count_missing_price': len(missing_price_variations),
                'missing_price_variation_ids': '|'.join(str(v['id']) for v in missing_price_variations),
                'skip_reason': '; '.join(filter(None, [
                    'BROKEN_VARIATION_SKIPPED - empty option value in source WooCommerce' if skipped_variations else '',
                    'MISSING_PRICE_SKIPPED - no source price (never fabricated as 0.00)' if missing_price_variations else '',
                ])),
                'quarantine_status': quarantine_status,
                'import_eligibility': eligibility,
            })

    print(json.dumps(counts, indent=2))
    print(f'Wrote {OUT_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
