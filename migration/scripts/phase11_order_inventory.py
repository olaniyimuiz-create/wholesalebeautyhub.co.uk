"""
Phase 11 source discovery: historical WooCommerce orders.

READ-ONLY. There is no Shopify mutation anywhere in this file - no orderCreate,
no transaction/refund mutation, no executor, no network call of any kind. It
reads migration/sql/dump.sql plus the committed Phase 9 / Phase 10 mapping
artifacts and writes four JSON reports.

PII: no customer email, phone, name or street address is written to any output.
Addresses are reduced to presence and country code. Orders are identified by
WooCommerce order ID only.

This store runs WooCommerce HPOS, so the authoritative order tables are
wp_wc_orders / wp_wc_order_operational_data / wp_wc_order_addresses, while line
items still live in the legacy wp_woocommerce_order_items(+itemmeta). The
wp_wc_order_*_lookup tables are analytics projections, read for cross-check
only and never treated as source of truth.

Usage: python migration/scripts/phase11_order_inventory.py
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
ORDER_TYPE_MAIN = 'shop_order'
ORDER_TYPE_REFUND = 'shop_order_refund'

WANTED = {
    'wp_wc_orders', 'wp_wc_order_operational_data', 'wp_wc_order_addresses',
    'wp_woocommerce_order_items', 'wp_woocommerce_order_itemmeta',
    'wp_wc_order_stats', 'wp_wc_order_product_lookup', 'wp_wc_order_tax_lookup',
    'wp_wc_order_coupon_lookup', 'wp_wc_orders_meta',
}

WANT_ITEMMETA = {
    '_product_id', '_variation_id', '_qty', '_line_subtotal', '_line_subtotal_tax',
    '_line_total', '_line_tax', '_refunded_item_id', 'method_id', '_cost',
    'discount_amount', 'discount_amount_tax',
}


def D(v):
    if v is None or v == '':
        return Decimal('0')
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal('0')


def money(d):
    return str(d.quantize(Decimal('0.01')))


def to_int(v):
    try:
        return int(D(v))
    except Exception:
        return 0


def main():
    if not os.path.isfile(SQL_DUMP_PATH):
        print(f'SOURCE ABSENT: {SQL_DUMP_PATH} - no fabricated result.')
        return 2

    orders, opdata = {}, {}
    addr_presence = collections.defaultdict(dict)
    items = {}
    itemmeta = collections.defaultdict(dict)
    stats_rows = 0
    lookup_rows = 0
    tax_lookup = collections.defaultdict(lambda: [Decimal('0'), Decimal('0')])
    coupon_lookup = collections.defaultdict(Decimal)
    meta_keys = collections.Counter()

    print('Streaming dump.sql (single pass, order tables only)...')
    scanned = 0
    for table, r in iter_insert_rows(SQL_DUMP_PATH, WANTED):
        scanned += 1
        if scanned % 1_000_000 == 0:
            print(f'  ...{scanned:,} order-table rows')

        if table == 'wp_wc_orders':
            oid = to_int(r.get('id'))
            orders[oid] = {
                'id': oid,
                'status': r.get('status') or '',
                'currency': r.get('currency') or '',
                'type': r.get('type') or '',
                'tax_amount': D(r.get('tax_amount')),
                'total_amount': D(r.get('total_amount')),
                'customer_id': to_int(r.get('customer_id')),
                'has_billing_email': bool(r.get('billing_email')),
                'date_created_gmt': r.get('date_created_gmt'),
                'date_updated_gmt': r.get('date_updated_gmt'),
                'parent_order_id': to_int(r.get('parent_order_id')),
                'payment_method': r.get('payment_method') or '',
                'has_transaction_id': bool(r.get('transaction_id')),
            }
        elif table == 'wp_wc_order_operational_data':
            oid = to_int(r.get('order_id'))
            if oid:
                opdata[oid] = {
                    'created_via': r.get('created_via') or '',
                    'prices_include_tax': r.get('prices_include_tax'),
                    'date_paid_gmt': r.get('date_paid_gmt'),
                    'date_completed_gmt': r.get('date_completed_gmt'),
                    'shipping_tax_amount': D(r.get('shipping_tax_amount')),
                    'shipping_total_amount': D(r.get('shipping_total_amount')),
                    'discount_tax_amount': D(r.get('discount_tax_amount')),
                    'discount_total_amount': D(r.get('discount_total_amount')),
                }
        elif table == 'wp_wc_order_addresses':
            oid = to_int(r.get('order_id'))
            if oid:
                addr_presence[oid][r.get('address_type') or '?'] = (r.get('country') or '')[:2]
        elif table == 'wp_woocommerce_order_items':
            items[to_int(r.get('order_item_id'))] = (to_int(r.get('order_id')),
                                                     r.get('order_item_type') or '')
        elif table == 'wp_woocommerce_order_itemmeta':
            k = r.get('meta_key')
            if k in WANT_ITEMMETA:
                itemmeta[to_int(r.get('order_item_id'))][k] = r.get('meta_value')
        elif table == 'wp_wc_order_stats':
            stats_rows += 1
        elif table == 'wp_wc_order_product_lookup':
            lookup_rows += 1
        elif table == 'wp_wc_order_tax_lookup':
            t = tax_lookup[to_int(r.get('order_id'))]
            t[0] += D(r.get('order_tax'))
            t[1] += D(r.get('shipping_tax'))
        elif table == 'wp_wc_order_coupon_lookup':
            coupon_lookup[to_int(r.get('order_id'))] += D(r.get('discount_amount'))
        elif table == 'wp_wc_orders_meta':
            meta_keys[r.get('meta_key') or ''] += 1

    print(f'  scanned {scanned:,} order-table rows')

    main_orders = {k: v for k, v in orders.items() if v['type'] == ORDER_TYPE_MAIN}
    refunds = {k: v for k, v in orders.items() if v['type'] == ORDER_TYPE_REFUND}
    other_types = collections.Counter(v['type'] for v in orders.values()
                                      if v['type'] not in (ORDER_TYPE_MAIN, ORDER_TYPE_REFUND))

    by_order = collections.defaultdict(list)
    for iid, (oid, itype) in items.items():
        by_order[oid].append((iid, itype))

    refunds_by_parent = collections.defaultdict(list)
    for r in refunds.values():
        refunds_by_parent[r['parent_order_id']].append(r)

    # ---------------- mappings ----------------
    cust_map = set()
    ledger = os.path.join(REPORTS, 'phase10_bulk_import_ledger.jsonl')
    if os.path.isfile(ledger):
        for line in open(ledger, encoding='utf-8'):
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get('shopify_customer_gid') and d.get('status') in (
                    'CREATED', 'CREATED_PHONE_DROPPED', 'SKIPPED_ALREADY_PRESENT'):
                cust_map.add(to_int(d.get('woo_customer_id')))

    imported_products = set()
    mpath = os.path.join(REPORTS, 'phase9_final_import_manifest.csv')
    if os.path.isfile(mpath):
        for r in csv.DictReader(open(mpath, encoding='utf-8')):
            if r.get('import_eligibility') == 'ALREADY_IMPORTED':
                imported_products.add(to_int(r['woo_product_id']))

    variant_map = set()
    vpath = os.path.join(REPORTS, 'phase9_variant_mapping.csv')
    if os.path.isfile(vpath):
        for r in csv.DictReader(open(vpath, encoding='utf-8')):
            if r.get('woo_variation_id'):
                variant_map.add(to_int(r['woo_variation_id']))

    print(f'  maps: customers={len(cust_map):,} products_live={len(imported_products)} '
          f'variants={len(variant_map)}')

    # ---------------- per-order ----------------
    per_order = {}
    for oid, o in main_orders.items():
        lines, ship, fees, coupons = [], Decimal('0'), Decimal('0'), Decimal('0')
        n_ship = n_fee = n_coupon = 0
        for iid, itype in by_order.get(oid, []):
            mm = itemmeta.get(iid, {})
            if itype == 'line_item':
                lines.append({
                    'product_id': to_int(mm.get('_product_id')),
                    'variation_id': to_int(mm.get('_variation_id')),
                    'qty': to_int(mm.get('_qty')),
                    'subtotal': D(mm.get('_line_subtotal')),
                    'total': D(mm.get('_line_total')),
                    'tax': D(mm.get('_line_tax')),
                })
            elif itype == 'shipping':
                ship += D(mm.get('_cost')); n_ship += 1
            elif itype == 'fee':
                fees += D(mm.get('_line_total')); n_fee += 1
            elif itype == 'coupon':
                coupons += D(mm.get('discount_amount')); n_coupon += 1
        per_order[oid] = {
            'order': o, 'op': opdata.get(oid, {}), 'lines': lines,
            'shipping': ship, 'fees': fees, 'coupons': coupons,
            'n_shipping': n_ship, 'n_fees': n_fee, 'n_coupons': n_coupon,
            'refunds': refunds_by_parent.get(oid, []),
        }

    # ---------------- population ----------------
    by_status = collections.Counter(o['status'] for o in main_orders.values())
    by_currency = collections.Counter(o['currency'] for o in main_orders.values())
    by_year = collections.Counter((o['date_created_gmt'] or '????')[:4] for o in main_orders.values())
    registered = sum(1 for o in main_orders.values() if o['customer_id'] > 0)
    total_lines = sum(len(v['lines']) for v in per_order.values())
    total_qty = sum(l['qty'] for v in per_order.values() for l in v['lines'])
    dates = sorted(o['date_created_gmt'] for o in main_orders.values() if o['date_created_gmt'])

    # ---------------- dependency ----------------
    dep = collections.Counter()
    missing_cust, missing_prod, missing_var = set(), set(), set()
    affected_lines, affected_value = 0, Decimal('0')
    for oid, v in per_order.items():
        cid = v['order']['customer_id']
        bad_p = [l for l in v['lines'] if l['product_id'] and l['product_id'] not in imported_products]
        bad_v = [l for l in v['lines'] if l['variation_id'] and l['variation_id'] not in variant_map]
        if cid == 0:
            dep['guest_no_customer_id'] += 1
        elif cid not in cust_map:
            dep['missing_customer_mapping'] += 1
            missing_cust.add(oid)
        else:
            dep['customer_mapped'] += 1
        if bad_p:
            dep['missing_product_mapping'] += 1
            missing_prod.add(oid)
        if bad_v:
            dep['missing_variant_mapping'] += 1
            missing_var.add(oid)
        if bad_p or bad_v:
            affected_lines += len(bad_p) + len(bad_v)
            affected_value += sum((l['total'] for l in bad_p + bad_v), Decimal('0'))
        if v['lines'] and not bad_p and not bad_v and (cid == 0 or cid in cust_map):
            dep['fully_mapped'] += 1

    # ---------------- financial ----------------
    def bucket():
        return {k: Decimal('0') for k in
                ('subtotal', 'discount', 'shipping', 'tax', 'fees', 'refunds', 'total')}
    fin_total = bucket()
    fin_year = collections.defaultdict(bucket)
    fin_status = collections.defaultdict(bucket)
    fin_cur = collections.defaultdict(bucket)
    for oid, v in per_order.items():
        o, op = v['order'], v['op']
        sub = sum((l['subtotal'] for l in v['lines']), Decimal('0'))
        disc = op.get('discount_total_amount', Decimal('0'))
        shipv = op.get('shipping_total_amount', v['shipping'])
        refunded = sum((abs(r['total_amount']) for r in v['refunds']), Decimal('0'))
        y = (o['date_created_gmt'] or '????')[:4]
        for tgt in (fin_total, fin_year[y], fin_status[o['status']], fin_cur[o['currency']]):
            tgt['subtotal'] += sub
            tgt['discount'] += disc
            tgt['shipping'] += shipv
            tgt['tax'] += o['tax_amount']
            tgt['fees'] += v['fees']
            tgt['refunds'] += refunded
            tgt['total'] += o['total_amount']

    # ---------------- refunds ----------------
    ref_class = collections.Counter()
    for oid, v in per_order.items():
        rs = v['refunds']
        if not rs:
            ref_class['no_refund'] += 1
        elif len(rs) > 1:
            ref_class['multiple_refunds'] += 1
        else:
            amt = abs(rs[0]['total_amount'])
            if o and v['order']['total_amount'] and amt >= v['order']['total_amount']:
                ref_class['full_refund'] += 1
            else:
                ref_class['partial_refund'] += 1

    # ---------------- exceptions ----------------
    exc = collections.defaultdict(lambda: {'orders': 0, 'value': Decimal('0')})
    mismatch_examples = []
    for oid, v in per_order.items():
        o = v['order']
        tags = []
        if not v['lines']:
            tags.append('MISSING_ORDER_DATA')
        if o['customer_id'] > 0 and o['customer_id'] not in cust_map:
            tags.append('MISSING_CUSTOMER')
        if any(l['product_id'] and l['product_id'] not in imported_products for l in v['lines']):
            tags.append('MISSING_PRODUCT')
        if any(l['variation_id'] and l['variation_id'] not in variant_map for l in v['lines']):
            tags.append('MISSING_VARIANT')
        if o['currency'] and o['currency'] != 'GBP':
            tags.append('UNSUPPORTED_CURRENCY')
        if len(v['refunds']) > 1:
            tags.append('REFUND_COMPLEX')
        if not o['payment_method']:
            tags.append('PAYMENT_AMBIGUOUS')
        sub = sum((l['subtotal'] for l in v['lines']), Decimal('0'))
        recomputed = (sub
                      - v['op'].get('discount_total_amount', Decimal('0'))
                      + v['op'].get('shipping_total_amount', Decimal('0'))
                      + o['tax_amount'] + v['fees'])
        delta = recomputed - o['total_amount']
        if abs(delta) > Decimal('0.01'):
            tags.append('FINANCIAL_MISMATCH')
            if len(mismatch_examples) < 15:
                mismatch_examples.append({'order_id': oid, 'recomputed': money(recomputed),
                                          'source_total': money(o['total_amount']),
                                          'delta': money(delta)})
        if not tags:
            tags = ['IMPORT_READY']
        for t in tags:
            exc[t]['orders'] += 1
            exc[t]['value'] += o['total_amount']

    os.makedirs(REPORTS, exist_ok=True)

    json.dump({
        'generated_from': SQL_DUMP_PATH,
        'order_table_rows_scanned': scanned,
        'authoritative_tables': {
            'orders': 'wp_wc_orders (HPOS)',
            'operational': 'wp_wc_order_operational_data',
            'addresses': 'wp_wc_order_addresses',
            'line_items': 'wp_woocommerce_order_items + wp_woocommerce_order_itemmeta',
            'analytics_crosscheck_only': ['wp_wc_order_stats', 'wp_wc_order_product_lookup',
                                          'wp_wc_order_tax_lookup', 'wp_wc_order_coupon_lookup'],
        },
        'counts': {
            'wp_wc_orders_rows_all_types': len(orders),
            'shop_order': len(main_orders),
            'shop_order_refund': len(refunds),
            'other_types': dict(other_types),
            'wp_wc_order_stats_rows': stats_rows,
            'wp_wc_order_product_lookup_rows': lookup_rows,
            'order_item_rows': len(items),
            'line_items': total_lines,
            'total_quantity': total_qty,
            'orders_with_no_line_items': sum(1 for v in per_order.values() if not v['lines']),
        },
        'population': {
            'by_status': dict(by_status),
            'by_currency': dict(by_currency),
            'by_year': dict(sorted(by_year.items())),
            'registered': registered,
            'guest': len(main_orders) - registered,
            'orders_with_variation_lines': sum(1 for v in per_order.values()
                                               if any(l['variation_id'] for l in v['lines'])),
            'orders_simple_products_only': sum(1 for v in per_order.values()
                                               if v['lines'] and not any(l['variation_id'] for l in v['lines'])),
            'orders_with_tax': sum(1 for o in main_orders.values() if o['tax_amount'] > 0),
            'orders_with_shipping_line': sum(1 for v in per_order.values() if v['n_shipping']),
            'orders_with_fee_line': sum(1 for v in per_order.values() if v['n_fees']),
            'orders_with_coupon_line': sum(1 for v in per_order.values() if v['n_coupons']),
            'orders_with_transaction_id': sum(1 for o in main_orders.values() if o['has_transaction_id']),
            'orders_with_payment_method': sum(1 for o in main_orders.values() if o['payment_method']),
            'date_min': dates[0] if dates else None,
            'date_max': dates[-1] if dates else None,
        },
        'order_meta_keys_top20': dict(meta_keys.most_common(20)),
        'address_presence': {
            'orders_with_billing_row': sum(1 for v in addr_presence.values() if 'billing' in v),
            'orders_with_shipping_row': sum(1 for v in addr_presence.values() if 'shipping' in v),
            'distinct_billing_countries': sorted({v.get('billing') for v in addr_presence.values()
                                                  if v.get('billing')}),
        },
        'PII_NOTE': 'No email, phone, name or street address appears in this report.',
    }, open(os.path.join(REPORTS, 'phase11_source_inventory.json'), 'w'), indent=2)

    json.dump({
        'customer_map_source': 'reports/phase10_bulk_import_ledger.jsonl',
        'customer_map_size': len(cust_map),
        'product_map_source': 'reports/phase9_final_import_manifest.csv (ALREADY_IMPORTED)',
        'product_map_size': len(imported_products),
        'variant_map_source': 'reports/phase9_variant_mapping.csv',
        'variant_map_size': len(variant_map),
        'orders_total': len(main_orders),
        'classification': dict(dep),
        'orders_missing_customer_mapping': len(missing_cust),
        'orders_missing_product_mapping': len(missing_prod),
        'orders_missing_variant_mapping': len(missing_var),
        'line_items_affected': affected_lines,
        'financial_value_affected': money(affected_value),
        'sample_missing_product_order_ids': sorted(missing_prod)[:25],
        'sample_missing_customer_order_ids': sorted(missing_cust)[:25],
    }, open(os.path.join(REPORTS, 'phase11_dependency_reconciliation.json'), 'w'), indent=2)

    def dump_b(b):
        return {k: money(v) for k, v in b.items()}
    json.dump({
        'note': 'Source values preserved verbatim; no normalization applied. Decimal arithmetic '
                'throughout, quantized to 2dp only on output.',
        'totals': dump_b(fin_total),
        'by_year': {k: dump_b(v) for k, v in sorted(fin_year.items())},
        'by_status': {k: dump_b(v) for k, v in sorted(fin_status.items())},
        'by_currency': {k: dump_b(v) for k, v in sorted(fin_cur.items())},
        'refund_classification': dict(ref_class),
        'tax_model_observed': {
            'order_level_tax': 'wp_wc_orders.tax_amount',
            'shipping_tax': 'wp_wc_order_operational_data.shipping_tax_amount',
            'discount_tax': 'wp_wc_order_operational_data.discount_tax_amount',
            'line_level_tax': '_line_tax / _line_subtotal_tax in order itemmeta',
            'prices_include_tax_flag': 'wp_wc_order_operational_data.prices_include_tax',
        },
        'financial_mismatch_examples': mismatch_examples,
    }, open(os.path.join(REPORTS, 'phase11_financial_inventory.json'), 'w'), indent=2)

    em = {k: {'orders': v['orders'], 'value': money(v['value'])} for k, v in sorted(exc.items())}
    json.dump({
        'note': 'Tags are not mutually exclusive; an order may carry several. IMPORT_READY is '
                'assigned only when no other tag applies.',
        'matrix': em,
    }, open(os.path.join(REPORTS, 'phase11_exception_matrix.json'), 'w'), indent=2)

    print('\n=== ORDER POPULATION ===')
    print(f'  shop_order        : {len(main_orders):,}')
    print(f'  shop_order_refund : {len(refunds):,}')
    print(f'  other types       : {dict(other_types)}')
    print(f'  dates             : {dates[0] if dates else None} -> {dates[-1] if dates else None}')
    print(f'  by status         : {dict(by_status)}')
    print(f'  by currency       : {dict(by_currency)}')
    print(f'  registered/guest  : {registered:,} / {len(main_orders)-registered:,}')
    print(f'  line items / qty  : {total_lines:,} / {total_qty:,}')
    print('\n=== DEPENDENCY ===')
    for k, v in sorted(dep.items()):
        print(f'  {k:<28} {v:,}')
    print(f'  line_items_affected          {affected_lines:,}   value {money(affected_value)}')
    print('\n=== FINANCIAL (all currencies pooled) ===')
    for k, v in fin_total.items():
        print(f'  {k:<10} {money(v):>16}')
    print('\n=== REFUNDS ===')
    for k, v in sorted(ref_class.items()):
        print(f'  {k:<20} {v:,}')
    print('\n=== EXCEPTION MATRIX ===')
    for k, v in em.items():
        print(f'  {k:<24} orders={v["orders"]:>6}  value={v["value"]:>14}')
    print('\nSHOPIFY MUTATIONS: 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
