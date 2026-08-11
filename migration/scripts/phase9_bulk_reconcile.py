"""
Phase 9.7 Step 5: independent live reconciliation for the bulk import.

Same principle as phase9_test_reconcile.py (fresh Shopify queries, never
the importer's own report) generalized to cover every record accumulated
so far in reports/phase9_bulk_import_result.json across all batches, not
just one fixed 9-row set. Field-comparison logic (expected_variants,
expected_inventory, cmp_row) is imported unchanged, not re-derived.

Read-only. Performs zero mutations.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request
from phase9_dry_run import load_products, to_number
from phase9_test_import import build_tags
from phase9_test_reconcile import fetch_product, expected_inventory, expected_variants, cmp_row

REPORTS_DIR = 'reports'
RESULT_PATH = os.path.join(REPORTS_DIR, 'phase9_bulk_import_result.json')
OUT_PATH = os.path.join(REPORTS_DIR, 'phase9_bulk_import_reconciliation.csv')


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - cannot reconcile without live credentials.')
        return 2

    if not os.path.exists(RESULT_PATH):
        print(f'STOP: {RESULT_PATH} does not exist - no batch has been run yet.')
        return 1

    with open(RESULT_PATH, encoding='utf-8') as f:
        all_batches = json.load(f)

    products = load_products()
    by_id = {p['id']: p for p in products}

    action_by_id = {}
    for batch in all_batches:
        for r in batch['records']:
            action_by_id[r['woo_id']] = r  # later batch wins if ever re-processed

    rows = []
    seen_gids = {}
    duplicate_gid_ids = []

    for woo_id, record in sorted(action_by_id.items()):
        product = by_id[woo_id]

        if record['action'] == 'FAILED':
            cmp_row(rows, woo_id, record.get('shopify_gid'), product['handle'], 'import_status', 'CREATED_or_UPDATED', 'FAILED',
                    notes=str(record.get('error', '')))
            continue

        gid = record['shopify_gid']
        if gid in seen_gids and seen_gids[gid] != woo_id:
            duplicate_gid_ids.append((woo_id, seen_gids[gid], gid))
        seen_gids[gid] = woo_id

        try:
            live = fetch_product(domain, token, api_version, gid)
        except Exception as e:
            cmp_row(rows, woo_id, gid, product['handle'], 'live_fetch', 'success', f'FAILED: {e}')
            continue

        cmp_row(rows, woo_id, gid, product['handle'], 'title', product['title'], live['title'])
        cmp_row(rows, woo_id, gid, product['handle'], 'handle', product['handle'], live['handle'])
        expected_status = 'ACTIVE' if product['status'] == 'publish' else 'DRAFT'
        cmp_row(rows, woo_id, gid, product['handle'], 'status', expected_status, live['status'])
        cmp_row(rows, woo_id, gid, product['handle'], 'vendor', product['vendor'], live['vendor'])
        cmp_row(rows, woo_id, gid, product['handle'], 'product_type', product.get('product_type', ''), live['productType'])
        expected_tags = sorted(build_tags(product))
        cmp_row(rows, woo_id, gid, product['handle'], 'tags', '|'.join(expected_tags), '|'.join(sorted(live['tags'])))
        live_legacy = live['metafield']['value'] if live['metafield'] else None
        cmp_row(rows, woo_id, gid, product['handle'], 'legacy_woo_id_metafield', str(woo_id), live_legacy)

        expected_images = len([u for u in product['images'] if not u.lower().endswith('.avif')])
        cmp_row(rows, woo_id, gid, product['handle'], 'image_count', expected_images, len(live['media']['edges']),
                notes='AVIF source images intentionally excluded' if any(u.lower().endswith('.avif') for u in product['images']) else '')

        exp_variants = expected_variants(product)
        live_variants = live['variants']['edges']
        unresolved_errors = record.get('variant_errors') or record.get('variant_retry_errors')
        cmp_row(rows, woo_id, gid, product['handle'], 'variant_count', len(exp_variants), len(live_variants),
                notes=str(unresolved_errors) if unresolved_errors else '')

        if unresolved_errors:
            cmp_row(rows, woo_id, gid, product['handle'], 'variant_price_sku', 'set correctly', 'NOT SET - variant mutation failed',
                    notes=str(unresolved_errors))
        else:
            live_by_option = {}
            for edge in live_variants:
                opt_val = edge['node']['selectedOptions'][0]['value'] if edge['node']['selectedOptions'] else None
                live_by_option[opt_val] = edge['node']
            for ev in exp_variants:
                lv = live_by_option.get(ev['option']) if ev['option'] else (live_variants[0]['node'] if live_variants else None)
                if not lv:
                    cmp_row(rows, woo_id, gid, product['handle'], f"variant[{ev['option']}].found", 'yes', 'NOT FOUND')
                    continue
                suffix = f"[{ev['option']}]" if ev['option'] else ''
                cmp_row(rows, woo_id, gid, product['handle'], f'variant{suffix}.price', ev['price'], to_number(lv['price']))
                cmp_row(rows, woo_id, gid, product['handle'], f'variant{suffix}.compare_at_price',
                        ev['compare_at_price'], to_number(lv['compareAtPrice']) if lv['compareAtPrice'] else None)
                cmp_row(rows, woo_id, gid, product['handle'], f'variant{suffix}.sku',
                        ev['sku'] or None, lv['inventoryItem']['sku'] or None)
                if ev['inventory'] is not None:
                    cmp_row(rows, woo_id, gid, product['handle'], f'variant{suffix}.inventory_quantity',
                            ev['inventory'], lv['inventoryQuantity'])

    if duplicate_gid_ids:
        for woo_id, other_woo_id, gid in duplicate_gid_ids:
            cmp_row(rows, woo_id, gid, '', 'DUPLICATE_GID_DETECTED', 'unique', f'shared with woo_id {other_woo_id}')

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woo_product_id', 'shopify_product_gid', 'handle', 'field', 'expected_value', 'actual_value', 'match', 'notes'])
        w.writerows(rows)

    total = len(rows)
    mismatches = [r for r in rows if r[6] == 'N']
    print(f'Reconciliation over {len(action_by_id)} processed product(s) so far: {total} field-level comparisons, '
          f'{len(mismatches)} mismatches, {total - len(mismatches)} matches, {len(duplicate_gid_ids)} duplicate GID(s).')
    for r in mismatches:
        print('MISMATCH:', r)
    print(f'\nWrote {OUT_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
