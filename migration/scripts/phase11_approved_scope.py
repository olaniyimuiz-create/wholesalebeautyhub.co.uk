"""
Phase 11 approved-scope computation (Gate O-3 outcome).

READ-ONLY. Imports no HTTP library; issues no Shopify call and no mutation.
Applies the decisions the project owner recorded on 2026-08-24 and emits the
resulting import cohort so it can be reviewed as data before anything is built.

Decisions applied:
  D-1  Migrate all statuses EXCEPT wc-checkout-draft            -> 8,207 orders
  D-2  Guest orders (customer_id = 0): email only, no customer link
  D-4  Line items whose product is absent from Shopify: import with
       productId = null, retaining title/sku/price
  D-5  Orphan variations: supply productId, omit variantId

Still OPEN and therefore NOT applied - these orders are held as
PENDING_DECISION rather than silently included or excluded:
  D-3  126 orders whose registered customer has no Shopify GID
  D-6  4 non-GBP orders
  D-7  22 orders with pre-existing source financial variance
  D-9  4 REFUND_COMPLEX orders

PII: order IDs and aggregates only. No email, name, phone or address.

Usage: python migration/scripts/phase11_approved_scope.py
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
WANTED = {'wp_wc_orders', 'wp_wc_order_operational_data',
          'wp_woocommerce_order_items', 'wp_woocommerce_order_itemmeta'}
WANT_META = {'_product_id', '_variation_id', '_qty', '_line_subtotal', '_line_total'}
EXCLUDED_STATUS = {'wc-checkout-draft'}          # D-1


def D(v):
    try:
        return Decimal(str(v)) if v not in (None, '') else Decimal('0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def I(v):
    try:
        return int(D(v))
    except Exception:
        return 0


def money(d):
    return str(d.quantize(Decimal('0.01')))


def main():
    if not os.path.isfile(SQL_DUMP_PATH):
        print('SOURCE ABSENT - no fabricated result.')
        return 2

    man = {I(r['woo_product_id']): r for r in
           csv.DictReader(open(os.path.join(REPORTS, 'phase9_final_import_manifest.csv'), encoding='utf-8'))}
    imported = {p for p, r in man.items() if r['import_eligibility'] == 'ALREADY_IMPORTED'}
    variant_map = set()
    with open(os.path.join(REPORTS, 'phase9_variant_mapping.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r.get('woo_variation_id'):
                variant_map.add(I(r['woo_variation_id']))
    mapped_customers = set()
    with open(os.path.join(REPORTS, 'phase10_bulk_import_ledger.jsonl'), encoding='utf-8') as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if d.get('shopify_customer_gid'):
                    mapped_customers.add(I(d.get('woo_customer_id')))

    orders, op, items, meta = {}, {}, {}, collections.defaultdict(dict)
    refund_parent = collections.defaultdict(list)
    print('Streaming dump.sql...')
    n = 0
    for t, r in iter_insert_rows(SQL_DUMP_PATH, WANTED):
        n += 1
        if t == 'wp_wc_orders':
            typ = r.get('type') or ''
            if typ == 'shop_order':
                orders[I(r.get('id'))] = {
                    'status': r.get('status') or '', 'currency': r.get('currency') or '',
                    'total': D(r.get('total_amount')), 'tax': D(r.get('tax_amount')),
                    'customer_id': I(r.get('customer_id')),
                    'payment_method': r.get('payment_method') or '',
                    'date': r.get('date_created_gmt'),
                }
            elif typ == 'shop_order_refund':
                refund_parent[I(r.get('parent_order_id'))].append(abs(D(r.get('total_amount'))))
        elif t == 'wp_wc_order_operational_data':
            op[I(r.get('order_id'))] = {
                'ship': D(r.get('shipping_total_amount')),
                'disc': D(r.get('discount_total_amount')),
            }
        elif t == 'wp_woocommerce_order_items':
            if (r.get('order_item_type') or '') == 'line_item':
                items[I(r.get('order_item_id'))] = I(r.get('order_id'))
        elif t == 'wp_woocommerce_order_itemmeta':
            k = r.get('meta_key')
            if k in WANT_META:
                meta[I(r.get('order_item_id'))][k] = r.get('meta_value')
    print(f'  scanned {n:,} rows')

    lines_by_order = collections.defaultdict(list)
    for iid, oid in items.items():
        mm = meta.get(iid, {})
        lines_by_order[oid].append({
            'pid': I(mm.get('_product_id')), 'vid': I(mm.get('_variation_id')),
            'qty': I(mm.get('_qty')), 'sub': D(mm.get('_line_subtotal')),
            'tot': D(mm.get('_line_total')),
        })

    rows = []
    disp = collections.Counter()
    val = collections.defaultdict(Decimal)
    null_product_lines = 0
    omit_variant_lines = 0
    no_line_item_orders = []

    for oid, o in orders.items():
        lines = lines_by_order.get(oid, [])
        if not lines:
            no_line_item_orders.append(oid)
        holds = []
        # --- D-1 (applied) ---
        if o['status'] in EXCLUDED_STATUS:
            d = 'EXCLUDED_D1_checkout_draft'
        else:
            # --- open decisions become explicit holds, never silent choices ---
            if o['customer_id'] > 0 and o['customer_id'] not in mapped_customers:
                holds.append('D-3_unmapped_customer')
            if o['currency'] and o['currency'] != 'GBP':
                holds.append('D-6_non_gbp')
            if len(refund_parent.get(oid, [])) > 1:
                holds.append('D-9_refund_complex')
            sub = sum((l['sub'] for l in lines), Decimal('0'))
            o_op = op.get(oid, {})
            recomputed = sub - o_op.get('disc', Decimal('0')) + o_op.get('ship', Decimal('0')) + o['tax']
            if abs(recomputed - o['total']) > Decimal('0.01'):
                holds.append('D-7_source_financial_variance')
            if not lines:
                holds.append('NO_LINE_ITEMS')
            d = 'PENDING_DECISION' if holds else 'APPROVED_FOR_IMPORT'
        disp[d] += 1
        val[d] += o['total']

        # transformation flags from D-2 / D-4 / D-5 (applied)
        npl = sum(1 for l in lines if l['pid'] and l['pid'] not in imported)
        ovl = sum(1 for l in lines if l['vid'] and l['vid'] not in variant_map)
        if d == 'APPROVED_FOR_IMPORT':
            null_product_lines += npl
            omit_variant_lines += ovl
        rows.append({
            'woo_order_id': oid, 'status': o['status'], 'currency': o['currency'],
            'date': o['date'], 'total': money(o['total']),
            'disposition': d, 'holds': '|'.join(holds),
            'customer_link': 'EMAIL_ONLY_D2' if o['customer_id'] == 0 else (
                'ASSOCIATE_GID' if o['customer_id'] in mapped_customers else 'UNMAPPED'),
            'line_items': len(lines),
            'lines_with_null_product_D4': npl,
            'lines_omitting_variant_D5': ovl,
            'refunds': len(refund_parent.get(oid, [])),
        })

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, 'phase11_approved_scope.csv'), 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r['woo_order_id']))

    hold_counts = collections.Counter()
    for r in rows:
        for h in (r['holds'].split('|') if r['holds'] else []):
            hold_counts[h] += 1

    draft_ids = sorted(o for o, v in orders.items() if v['status'] == 'wc-checkout-draft')
    summary = {
        'decisions_applied': {
            'D-1': 'exclude wc-checkout-draft only',
            'D-2': 'guest orders: email only, no customer association',
            'D-4': 'absent products: productId=null, retain title/sku/price',
            'D-5': 'orphan variations: supply productId, omit variantId',
        },
        'decisions_still_open': ['D-3', 'D-6', 'D-7', 'D-9'],
        'disposition': {k: {'orders': v, 'value': money(val[k])} for k, v in disp.items()},
        'holds_breakdown': dict(hold_counts),
        'transformation_effects': {
            'line_items_with_null_productId': null_product_lines,
            'line_items_omitting_variantId': omit_variant_lines,
        },
        'checkout_draft_order_ids': draft_ids,
        'orders_with_no_line_items': sorted(no_line_item_orders),
        'no_line_item_orders_are_exactly_the_drafts':
            sorted(no_line_item_orders) == draft_ids,
        'PII_NOTE': 'Order IDs and aggregates only.',
    }
    json.dump(summary, open(os.path.join(REPORTS, 'phase11_approved_scope.json'), 'w'), indent=2)

    print('\n=== DISPOSITION UNDER APPROVED DECISIONS ===')
    for k, v in sorted(disp.items()):
        print(f'  {k:<32} {v:>6} orders   {money(val[k]):>14}')
    print('\n=== HOLDS (open decisions, not silently resolved) ===')
    for k, v in sorted(hold_counts.items()):
        print(f'  {k:<34} {v:>6}')
    print('\n=== TRANSFORMATION EFFECTS ===')
    print(f'  line items importing with productId=null : {null_product_lines}')
    print(f'  line items omitting variantId            : {omit_variant_lines}')
    print(f'\ncheckout-draft order ids: {draft_ids}')
    print(f'orders with no line items: {sorted(no_line_item_orders)}')
    print(f'-> identical sets: {sorted(no_line_item_orders) == draft_ids}')
    print('\nSHOPIFY MUTATIONS: 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
