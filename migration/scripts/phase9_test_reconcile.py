"""
Phase 9 test-import reconciliation: compares WooCommerce source data
(migration/data/products.json) against what the Shopify test store
actually reports back via a fresh, real, read-only Admin API query - not
against what the import script claimed to have done. This is the
distinction reports/phase9_reconciliation.csv (Phase 9 dry run) already
documents: that file compares source to a locally-transformed payload and
explicitly never contacted Shopify. This script is the real thing.

Writes reports/phase9_test_import_reconciliation.csv using the schema
already designed in reports/phase9_reconciliation_template.csv (long
format: one row per product/field comparison), rather than overwriting
either of the two pre-existing, differently-scoped reconciliation files.

Read-only. Performs zero mutations.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request
from phase9_dry_run import load_products, to_number
from phase9_test_import import read_test_set_ids, build_tags, find_existing_by_legacy_id

REPORTS_DIR = 'reports'
OUT_PATH = os.path.join(REPORTS_DIR, 'phase9_test_import_reconciliation.csv')


def fetch_product(domain, token, api_version, gid):
    query = '''
    {
      product(id: "%s") {
        id handle title status vendor productType tags descriptionHtml
        metafield(namespace: "custom", key: "legacy_woo_id") { value }
        media(first: 10) { edges { node { id } } }
        variants(first: 50) {
          edges { node { id sku price compareAtPrice selectedOptions { name value } inventoryItem { sku } inventoryQuantity } }
        }
      }
    }
    ''' % gid
    data = graphql_request(domain, token, api_version, query)
    if 'errors' in data:
        raise RuntimeError(str(data['errors']))
    return data['data']['product']


def expected_inventory(manage_stock, stock_quantity):
    if manage_stock != 'yes':
        return None  # untracked - Shopify inventoryQuantity is meaningless for an untracked item, not compared
    n = to_number(stock_quantity)
    return int(n) if n is not None else 0


def expected_variants(product):
    if product['wc_type'] == 'variable' and product['variations']:
        out = []
        for v in product['variations']:
            reg = to_number(v['regular_price'])
            price = to_number(v['price']) or reg
            compare = reg if reg and price and reg != price else None
            out.append({'sku': v['sku'], 'price': price, 'compare_at_price': compare, 'option': v['options'][0],
                        'inventory': expected_inventory(v['manage_stock'], v['stock_quantity'])})
        return out
    reg = to_number(product['regular_price'])
    price = to_number(product['price']) or reg
    compare = reg if reg and price and reg != price else None
    return [{'sku': product['sku'], 'price': price, 'compare_at_price': compare, 'option': None,
             'inventory': expected_inventory(product['manage_stock'], product['stock_quantity'])}]


def cmp_row(rows, woo_id, gid, handle, field, expected, actual, notes=''):
    match = 'Y' if str(expected) == str(actual) else 'N'
    rows.append([woo_id, gid or '', handle or '', field, expected, actual, match, notes])


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - cannot reconcile without live credentials.')
        return 2

    products = load_products()
    by_id = {p['id']: p for p in products}
    ids = read_test_set_ids()

    with open(os.path.join(REPORTS_DIR, 'phase9_test_import_result.json'), encoding='utf-8') as f:
        import_result = json.load(f)
    action_by_id = {r['woo_id']: r for r in import_result['records']}

    rows = []
    for woo_id in ids:
        product = by_id[woo_id]
        record = action_by_id.get(woo_id)

        if not record or record['action'] == 'QUARANTINED':
            reason = record['reason'] if record else 'not attempted'
            cmp_row(rows, woo_id, None, product['handle'], 'import_status', 'QUARANTINED', 'QUARANTINED', notes=reason)
            continue

        gid = record['shopify_gid']
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

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woo_product_id', 'shopify_product_gid', 'handle', 'field', 'expected_value', 'actual_value', 'match', 'notes'])
        w.writerows(rows)

    total = len(rows)
    mismatches = [r for r in rows if r[6] == 'N']
    print(f'Reconciliation: {total} field-level comparisons, {len(mismatches)} mismatches, {total - len(mismatches)} matches.')
    for r in mismatches:
        print('MISMATCH:', r)
    print(f'\nWrote {OUT_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
