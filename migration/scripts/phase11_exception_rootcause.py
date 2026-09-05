"""
Phase 11 exception root-cause analysis.

READ-ONLY. No Shopify contact of any kind - this file imports no HTTP library
and issues no mutation. It answers, from source data plus committed Phase 9/10
artifacts, *why* each exception class occurs, so the decision register rests on
causes rather than counts.

Questions answered:
  1. The 126 orders whose customer is unmapped - were those customers
     QUARANTINED by Phase 10, EXCLUDED as staff, FAILED at import, or absent
     from wp_wc_customer_lookup entirely?
  2. The 262 orders referencing products not live in Shopify - are those the 15
     Phase 9 quarantined products, or products deleted from WooCommerce before
     the export (never in the 611-row catalogue at all)?
  3. The 58 orders referencing unmapped variations - same split.

PII: emits IDs and aggregate counts only. No email, name, phone or address.

Usage: python migration/scripts/phase11_exception_rootcause.py
"""
import collections
import csv
import json
import os
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(__file__))
from sql_utils import iter_insert_rows
from database_parser import SQL_DUMP_PATH

REPORTS = 'reports'
WANTED = {'wp_wc_orders', 'wp_woocommerce_order_items', 'wp_woocommerce_order_itemmeta',
          'wp_wc_customer_lookup'}
WANT_META = {'_product_id', '_variation_id', '_line_total'}


def D(v):
    try:
        return Decimal(str(v)) if v not in (None, '') else Decimal('0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def to_int(v):
    try:
        return int(D(v))
    except Exception:
        return 0


def main():
    if not os.path.isfile(SQL_DUMP_PATH):
        print('SOURCE ABSENT - no fabricated result.')
        return 2

    # ---- committed maps ----
    man = {int(r['woo_product_id']): r for r in
           csv.DictReader(open(os.path.join(REPORTS, 'phase9_final_import_manifest.csv'), encoding='utf-8'))}
    imported = {p for p, r in man.items() if r['import_eligibility'] == 'ALREADY_IMPORTED'}
    quarantined_products = {p for p, r in man.items() if r['import_eligibility'] == 'QUARANTINE'}
    catalogue = set(man)

    variant_map = set()
    with open(os.path.join(REPORTS, 'phase9_variant_mapping.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r.get('woo_variation_id'):
                variant_map.add(to_int(r['woo_variation_id']))

    mapped_customers, failed_customers = set(), set()
    with open(os.path.join(REPORTS, 'phase10_bulk_import_ledger.jsonl'), encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            cid = to_int(d.get('woo_customer_id'))
            if d.get('shopify_customer_gid'):
                mapped_customers.add(cid)
            if d.get('status') == 'FAILED':
                failed_customers.add(cid)

    quarantined_customers = set()
    qpath = os.path.join(REPORTS, 'phase10_customer_quarantine.csv')
    if os.path.isfile(qpath):
        with open(qpath, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                for key in ('woo_customer_id', 'customer_id', 'user_id'):
                    v = (r.get(key) or '').strip()
                    if v.isdigit():
                        quarantined_customers.add(int(v))
                        break

    # ---- single streaming pass ----
    orders, items, itemmeta = {}, {}, collections.defaultdict(dict)
    lookup_customer_ids = set()
    print('Streaming dump.sql...')
    n = 0
    for table, r in iter_insert_rows(SQL_DUMP_PATH, WANTED):
        n += 1
        if n % 1_000_000 == 0:
            print(f'  ...{n:,} rows')
        if table == 'wp_wc_orders':
            if (r.get('type') or '') == 'shop_order':
                orders[to_int(r.get('id'))] = to_int(r.get('customer_id'))
        elif table == 'wp_woocommerce_order_items':
            if (r.get('order_item_type') or '') == 'line_item':
                items[to_int(r.get('order_item_id'))] = to_int(r.get('order_id'))
        elif table == 'wp_woocommerce_order_itemmeta':
            k = r.get('meta_key')
            if k in WANT_META:
                itemmeta[to_int(r.get('order_item_id'))][k] = r.get('meta_value')
        elif table == 'wp_wc_customer_lookup':
            lookup_customer_ids.add(to_int(r.get('customer_id')))
    print(f'  scanned {n:,} rows')

    # ---- 1. customer root cause ----
    unmapped_orders = {oid: cid for oid, cid in orders.items()
                       if cid > 0 and cid not in mapped_customers}
    cust_cause = collections.Counter()
    cause_ids = collections.defaultdict(set)
    for oid, cid in unmapped_orders.items():
        if cid in failed_customers:
            c = 'PHASE10_IMPORT_FAILED_invalid_email'
        elif cid in quarantined_customers:
            c = 'PHASE10_QUARANTINED'
        elif cid not in lookup_customer_ids:
            c = 'ABSENT_FROM_wp_wc_customer_lookup'
        else:
            c = 'IN_LOOKUP_BUT_NOT_IMPORTED_unexplained'
        cust_cause[c] += 1
        cause_ids[c].add(cid)

    # ---- 2/3. product & variant root cause ----
    prod_cause = collections.Counter()
    var_cause = collections.Counter()
    prod_value = collections.defaultdict(Decimal)
    bad_products, bad_variants = set(), set()
    orders_with_bad_product, orders_with_bad_variant = set(), set()
    for iid, oid in items.items():
        mm = itemmeta.get(iid, {})
        pid, vid = to_int(mm.get('_product_id')), to_int(mm.get('_variation_id'))
        val = D(mm.get('_line_total'))
        if pid and pid not in imported:
            orders_with_bad_product.add(oid)
            bad_products.add(pid)
            if pid in quarantined_products:
                c = 'PHASE9_QUARANTINED_product'
            elif pid in catalogue:
                c = 'IN_CATALOGUE_NOT_IMPORTED_unexplained'
            else:
                c = 'DELETED_FROM_WOO_never_in_catalogue'
            prod_cause[c] += 1
            prod_value[c] += val
        if vid and vid not in variant_map:
            orders_with_bad_variant.add(oid)
            bad_variants.add(vid)
            var_cause['VARIATION_NOT_IN_PHASE9_MAPPING' if pid in imported
                       else 'PARENT_PRODUCT_ALSO_UNMAPPED'] += 1

    out = {
        'note': 'Root causes for Phase 11 exception classes. IDs only, no PII.',
        'customer_exceptions': {
            'orders_affected': len(unmapped_orders),
            'distinct_customer_ids': len({c for c in unmapped_orders.values()}),
            'root_cause_by_order': dict(cust_cause),
            'distinct_customer_ids_by_cause': {k: len(v) for k, v in cause_ids.items()},
            'sample_ids_by_cause': {k: sorted(v)[:10] for k, v in cause_ids.items()},
        },
        'product_exceptions': {
            'orders_affected': len(orders_with_bad_product),
            'distinct_product_ids': len(bad_products),
            'root_cause_by_line_item': dict(prod_cause),
            'value_by_cause': {k: str(v.quantize(Decimal('0.01'))) for k, v in prod_value.items()},
            'sample_deleted_product_ids': sorted(bad_products - catalogue)[:20],
            'phase9_quarantined_hit': sorted(bad_products & quarantined_products),
        },
        'variant_exceptions': {
            'orders_affected': len(orders_with_bad_variant),
            'distinct_variation_ids': len(bad_variants),
            'root_cause_by_line_item': dict(var_cause),
        },
        'reference_sets': {
            'phase9_catalogue': len(catalogue),
            'phase9_imported': len(imported),
            'phase9_quarantined': len(quarantined_products),
            'phase9_variant_map': len(variant_map),
            'phase10_mapped_customers': len(mapped_customers),
            'phase10_quarantined_customers': len(quarantined_customers),
            'phase10_failed_customers': len(failed_customers),
            'wp_wc_customer_lookup_ids': len(lookup_customer_ids),
        },
    }
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, 'phase11_exception_rootcause.json')
    json.dump(out, open(path, 'w'), indent=2)

    print(f'\nWrote {path}')
    print('\n=== CUSTOMER EXCEPTIONS ===')
    for k, v in cust_cause.items():
        print(f'  {k:<42} orders={v:<6} distinct_ids={len(cause_ids[k])}')
    print('\n=== PRODUCT EXCEPTIONS ===')
    for k, v in prod_cause.items():
        print(f'  {k:<42} line_items={v:<6} value={prod_value[k].quantize(Decimal("0.01"))}')
    print(f'  distinct unmapped product ids: {len(bad_products)}')
    print(f'  of which Phase 9 quarantined : {len(bad_products & quarantined_products)}')
    print(f'  of which absent from catalogue: {len(bad_products - catalogue)}')
    print('\n=== VARIANT EXCEPTIONS ===')
    for k, v in var_cause.items():
        print(f'  {k:<42} line_items={v}')
    print('\nSHOPIFY MUTATIONS: 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
