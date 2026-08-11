"""
Phase 9 closeout, Step 12: collection-level readiness report - one row
per PLANNED collection (shopify/foundation/collections.json), not one row
per product (that's the existing phase9_collection_mapping.csv from
phase9_dry_run.py). Cross-references against live Shopify (read-only
query only) to confirm none exist yet, and against the current 611-product
catalog to compute how many of each collection's members are already
importable/imported vs quarantined.

Read-only. Performs zero mutations. Does not create any collection.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request
from phase9_dry_run import (
    load_products, load_collections, build_collection_lookup,
    normalize_name, resolve_category_name, CATEGORY_CLEANUP_MAP,
)

REPORTS_DIR = 'reports'
OUT_PATH = os.path.join(REPORTS_DIR, 'phase9_collection_readiness.csv')


def fetch_existing_collection_handles(domain, token, api_version):
    handles = set()
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        data = graphql_request(domain, token, api_version,
                                '{ collections(first: 50%s) { pageInfo { hasNextPage endCursor } edges { node { handle } } } }' % after)
        for edge in data['data']['collections']['edges']:
            handles.add(edge['node']['handle'])
        page = data['data']['collections']['pageInfo']
        if not page['hasNextPage']:
            return handles
        cursor = page['endCursor']


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - cannot confirm live collection state without credentials.')
        return 2

    existing_handles = fetch_existing_collection_handles(domain, token, api_version)
    print(f'{len(existing_handles)} collection(s) currently exist in Shopify: {sorted(existing_handles)}')

    with open('shopify/foundation/collections.json', encoding='utf-8') as f:
        planned = json.load(f)

    products = load_products()
    collection_lookup = build_collection_lookup(load_collections())

    manifest_rows = {}
    manifest_path = os.path.join(REPORTS_DIR, 'phase9_final_import_manifest.csv')
    with open(manifest_path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            manifest_rows[int(row['woo_product_id'])] = row['import_eligibility']

    def status_counts_for_handle(handle):
        counts = {'ALREADY_IMPORTED': 0, 'IMPORT': 0, 'QUARANTINE': 0, 'EXCLUDE': 0}
        for p in products:
            member = False
            for cat in p['categories']:
                if collection_lookup.get(normalize_name(resolve_category_name(cat))) == handle:
                    member = True
                    break
            if not member and p['vendor'] and collection_lookup.get(normalize_name(p['vendor'])) == handle:
                member = True
            if member:
                counts[manifest_rows.get(p['id'], 'EXCLUDE')] += 1
        return counts

    fields = ['handle', 'name', 'type', 'group_or_rule', 'planned_product_count',
              'exists_in_shopify', 'members_already_imported', 'members_import_ready',
              'members_quarantined', 'members_excluded', 'notes']

    all_planned = (
        [dict(c, category='category') for c in planned['category_collections']]
        + [dict(c, category='brand') for c in planned['brand_collections']]
        + [dict(c, category='manual_promo') for c in planned['manual_promo_collections']]
    )

    with open(OUT_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in all_planned:
            handle = c['handle']
            counts = status_counts_for_handle(handle)
            w.writerow({
                'handle': handle,
                'name': c['name'],
                'type': c['type'],
                'group_or_rule': c.get('group') or c.get('rule', ''),
                'planned_product_count': c.get('product_count', ''),
                'exists_in_shopify': 'YES' if handle in existing_handles else 'NO',
                'members_already_imported': counts['ALREADY_IMPORTED'],
                'members_import_ready': counts['IMPORT'],
                'members_quarantined': counts['QUARANTINE'],
                'members_excluded': counts['EXCLUDE'],
                'notes': '',
            })

        # Category-cleanup consolidation targets and "Uncategorized" (out of
        # architecture scope) are reported for visibility, not as planned
        # collection rows duplicating the above.
        w.writerow({
            'handle': '', 'name': 'Uncategorized (4 products)', 'type': 'informational',
            'group_or_rule': 'Explicitly out of the approved architecture - needs manual audit, not a 1:1 collection mapping',
            'planned_product_count': '', 'exists_in_shopify': '', 'members_already_imported': '',
            'members_import_ready': '', 'members_quarantined': '', 'members_excluded': '',
            'notes': 'Not created automatically; ADR-009 / SHOPIFY_FOUNDATION.md',
        })

    print(f'{len(all_planned)} planned collection(s) written to {OUT_PATH}')
    print(f'{len(existing_handles & {c["handle"] for c in all_planned})} of {len(all_planned)} planned collections already exist in Shopify (expected 0)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
