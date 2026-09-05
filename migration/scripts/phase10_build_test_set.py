"""Build the 10-customer Tier-3 test cohort. OFFLINE ONLY - creates nothing.

Selecting the cohort is preparation, not execution. This writes a CSV; it never
contacts Shopify, and authorizing the import that would consume it is Gate 6 in
ADR-014, which is open.

SELECTION
---------
Deterministic and criteria-driven, not a sample. Each slot exercises a distinct
path through the transformation, so a 10-record test actually covers the code
rather than covering ten similar customers:

  1  registered, GB address with a county      -> province DROPPED by the GB rule
  2  registered, non-GB address, valid code    -> province SENT
  3  guest, no address at all                  -> customerCreate only, no address call
  4  registered, address but NO country        -> SKIPPED_INVALID_COUNTRY, customer survives
  5  has a shipping address as well as billing -> exercises Option B's second call
  6  phone shared with another customer        -> phone OMITTED, customer still created
  7  unique phone                              -> phone SENT
  8  no woo_registered_at (guest)              -> one metafield, not two
  9  has woo_registered_at (registered)        -> both metafields
 10  company present on the address            -> company rides on the address, not the customer

Ties break on the lowest woo_customer_id so the cohort is reproducible.

DELIBERATELY EXCLUDED
---------------------
Any customer caught in the 247 name conflicts. Under the recommended
EXCLUDE_AFFECTED_CUSTOMERS policy they would not be in a real run either, and a
test cohort should not exercise a path the policy forbids.

OUTPUT
------
reports/phase10_test_import_set.csv - GITIGNORED. Ten real people's names,
emails, phones and addresses. Never tracked.

reports/phase10_test_set_summary.json - TRACKED. Coverage counts only.

Run: python migration/scripts/phase10_build_test_set.py
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_customer_dry_run as dr
import phase10_import_runtime as rt
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows

TEST_SET_PATH = os.path.join('reports', 'phase10_test_import_set.csv')
SUMMARY_PATH = os.path.join('reports', 'phase10_test_set_summary.json')

FIELDS = ['slot', 'selection_reason', 'woo_customer_id', 'email', 'first_name',
          'last_name', 'company', 'phone', 'phone_action', 'billing_address1',
          'billing_city', 'billing_province', 'billing_province_code_sent',
          'billing_zip', 'billing_country', 'has_shipping_address',
          'is_registered', 'date_registered', 'expected_address_calls',
          'expected_metafields']


def _refuse(*_a, **_k):
    raise SystemExit('REFUSED: phase10_build_test_set must never contact Shopify')


def load_population():
    data = load_dump(SQL_DUMP_PATH)
    usermeta, order_billing = data['usermeta'], data['order_billing_by_email']
    staff = {}
    for uid, meta in usermeta.items():
        caps = php_unserialize(meta.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            staff[uid] = set(caps)
    consent = dr.load_fluentcrm_consent()

    seen, imports, conflicted = {}, [], set()
    for _t, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_wc_customer_lookup'}):
        cand = dr.build_candidate(row, usermeta, order_billing)
        classification, reason, notes = dr.classify(cand, seen, staff, consent, set())
        if classification == 'QUARANTINE' and reason == 'duplicate_email_conflicting_identity':
            # The IMPORT row this conflicts with is named in the note.
            import re
            m = re.search(r'woo_customer_id=(\d+)', notes or '')
            if m:
                conflicted.add(int(m.group(1)))
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand
            uid = cand['user_id']
            profile_phone = (usermeta.get(uid, {}) or {}).get('billing_phone') if uid else None
            cand['phone_from_profile'] = bool((profile_phone or '').strip())
            imports.append(cand)
    return imports, conflicted


def main():
    dr.graphql_request = _refuse
    print('Reading dump.sql (customer tables only)...')
    imports, conflicted = load_population()

    collisions = rt.phone_collision_groups(
        [c for c in imports if (c.get('phone') or '').strip()])
    colliding_ids = {i for ids in collisions.values() for i in ids}

    eligible = [c for c in imports if c['woo_customer_id'] not in conflicted]
    eligible.sort(key=lambda c: c['woo_customer_id'])
    print(f'{len(imports)} IMPORT customers, {len(conflicted)} excluded for a name '
          f'conflict, {len(eligible)} eligible for the cohort')

    def has_billing(c):
        return bool((c.get('billing_address1') or '').strip())

    criteria = [
        ('GB address with a county - province dropped by the GB rule',
         lambda c: has_billing(c) and (c.get('billing_country') or '').upper() == 'GB'
         and (c.get('billing_province') or '').strip()),
        ('non-GB address with a valid province code - province sent',
         lambda c: has_billing(c) and (c.get('billing_country') or '').upper() not in ('GB', '')
         and rt.province_code_is_valid(c.get('billing_country'), c.get('billing_province'))),
        ('no address at all - customerCreate only',
         lambda c: not has_billing(c) and not c.get('has_shipping_address')),
        ('address with no country - SKIPPED_INVALID_COUNTRY, customer survives',
         lambda c: has_billing(c) and not (c.get('billing_country') or '').strip()),
        ('billing and shipping - exercises the second address call',
         lambda c: has_billing(c) and c.get('has_shipping_address')),
        ('phone shared with another customer - phone omitted',
         lambda c: c['woo_customer_id'] in colliding_ids),
        ('unique phone - phone sent',
         lambda c: (c.get('phone') or '').strip() and c['woo_customer_id'] not in colliding_ids),
        ('guest with no registration date - one metafield',
         lambda c: not c.get('is_registered') and not (c.get('date_registered') or '').strip()),
        ('registered with a registration date - both metafields',
         lambda c: c.get('is_registered') and (c.get('date_registered') or '').strip()),
        ('company on the address - company rides on the address, not the customer',
         lambda c: has_billing(c) and (c.get('company') or '').strip()),
    ]

    chosen, used, unmet = [], set(), []
    for slot, (reason, predicate) in enumerate(criteria, 1):
        pick = next((c for c in eligible
                     if c['woo_customer_id'] not in used and predicate(c)), None)
        if pick is None:
            unmet.append((slot, reason))
            print(f'  slot {slot}: NO CANDIDATE - {reason}')
            continue
        used.add(pick['woo_customer_id'])
        chosen.append((slot, reason, pick))

    rows = []
    for slot, reason, c in chosen:
        code, _flag = rt.validate_province_code(c.get('billing_country'),
                                                c.get('billing_province'))
        keeps_phone = (c['woo_customer_id'] not in colliding_ids
                       and bool((c.get('phone') or '').strip()))
        plan = rt.plan_customer_import(c, include_shipping=False,
                                       phone_allowed=keeps_phone)
        rows.append({
            'slot': slot, 'selection_reason': reason,
            'woo_customer_id': c['woo_customer_id'], 'email': c['email'],
            'first_name': c['first_name'], 'last_name': c['last_name'],
            'company': c.get('company') or '', 'phone': c.get('phone') or '',
            'phone_action': 'SEND_PHONE' if keeps_phone else 'OMIT_PHONE',
            'billing_address1': c.get('billing_address1') or '',
            'billing_city': c.get('billing_city') or '',
            'billing_province': c.get('billing_province') or '',
            'billing_province_code_sent': code or '',
            'billing_zip': c.get('billing_zip') or '',
            'billing_country': c.get('billing_country') or '',
            'has_shipping_address': c.get('has_shipping_address'),
            'is_registered': c.get('is_registered'),
            'date_registered': c.get('date_registered') or '',
            'expected_address_calls': sum(1 for st in plan
                                          if st['stage'] == rt.STAGE_ADDRESS),
            'expected_metafields': len(plan[0]['input']['metafields']),
        })

    os.makedirs('reports', exist_ok=True)
    with open(TEST_SET_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        'cohort_size': len(rows),
        'slots_unfilled': [{'slot': s, 'criterion': r} for s, r in unmet],
        'excluded_for_name_conflict': len(conflicted),
        'eligible_pool': len(eligible),
        'coverage': {
            'province_sent': sum(1 for r in rows if r['billing_province_code_sent']),
            'province_dropped': sum(1 for r in rows
                                    if r['billing_province'] and not r['billing_province_code_sent']),
            'phone_sent': sum(1 for r in rows if r['phone_action'] == 'SEND_PHONE'),
            'phone_omitted': sum(1 for r in rows if r['phone_action'] == 'OMIT_PHONE'),
            'with_address': sum(1 for r in rows if r['expected_address_calls'] > 0),
            'without_address': sum(1 for r in rows if r['expected_address_calls'] == 0),
            'two_metafields': sum(1 for r in rows if r['expected_metafields'] == 2),
            'one_metafield': sum(1 for r in rows if r['expected_metafields'] == 1),
        },
        'expected_mutations_if_authorized': {
            'customerCreate': len(rows),
            'customerAddressCreate': sum(r['expected_address_calls'] for r in rows),
            'total': len(rows) + sum(r['expected_address_calls'] for r in rows),
            'estimated_cost_points': (len(rows) + sum(r['expected_address_calls']
                                                      for r in rows)) * 10,
        },
        'authorization': 'NOT AUTHORIZED - ADR-014 Gate 6 is open. This file was '
                         'built, not executed. No Shopify record was created.',
    }
    blob = json.dumps(summary)
    for r in rows:
        for value in (r['email'], r['phone'], r['billing_address1'], r['first_name']):
            if value and str(value) in blob:
                raise SystemExit('ABORTED: PII reached the tracked summary')
    json.dump(summary, open(SUMMARY_PATH, 'w', encoding='utf-8'), indent=2, sort_keys=True)

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {TEST_SET_PATH} ({len(rows)} customers) - CONTAINS PII, gitignored')
    print(f'Wrote {SUMMARY_PATH} - coverage only, tracked')
    print('\nNothing was created in Shopify. Executing this cohort requires '
          'ADR-014 Gate 6.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
