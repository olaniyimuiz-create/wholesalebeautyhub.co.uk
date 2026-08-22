"""Phase 10 customer metafield readiness. OFFLINE ONLY - never contacts Shopify.

Ratified 2026-08-21:

  custom.legacy_woo_customer_id   YES - MANDATORY on every created customer
      namespace custom / key legacy_woo_customer_id / type single_line_text_field
      value     wp_wc_customer_lookup.customer_id

      Woo ID -> legacy_woo_customer_id -> Shopify Customer GID

  custom.woo_registered_at        RETAINED as a customer metafield
      Shopify's Customer.createdAt is server-controlled and read-only. It is
      never written, never manipulated, never approximated. The registration
      date lives in a metafield or nowhere.

This script proves both are satisfiable for the whole import population before
anything is written, and reports where the source data cannot supply a value.

Writes reports/phase10_metafield_readiness.json - TRACKED, aggregate only.
No per-customer file: there is nothing here a reviewer must act on
record-by-record, and the values are customer identifiers.

Run: python migration/scripts/phase10_metafield_readiness.py
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_customer_dry_run as dr
import phase10_import_runtime as rt
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows

SUMMARY_PATH = os.path.join('reports', 'phase10_metafield_readiness.json')


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_metafield_readiness must never contact Shopify')


def load_import_candidates():
    data = load_dump(SQL_DUMP_PATH)
    usermeta, order_billing = data['usermeta'], data['order_billing_by_email']
    staff = {}
    for uid, meta in usermeta.items():
        caps = php_unserialize(meta.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            staff[uid] = set(caps)
    consent = dr.load_fluentcrm_consent()

    seen, imports = {}, []
    for _table, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_wc_customer_lookup'}):
        cand = dr.build_candidate(row, usermeta, order_billing)
        classification, _reason, _notes = dr.classify(cand, seen, staff, consent, set())
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand
            imports.append(cand)
    return imports


def main():
    dr.graphql_request = _refuse

    print('Reading dump.sql (customer tables only)...')
    imports = load_import_candidates()

    legacy_present = 0
    legacy_ids = set()
    legacy_non_numeric = 0
    registered_at_present = 0
    registered_at_absent = []
    createdat_leaks = 0
    year_counts = collections.Counter()

    for cand in imports:
        payload = rt.build_customer_input(cand)
        rt.assert_legacy_metafield_present(payload)

        by_key = {(m['namespace'], m['key']): m for m in payload['metafields']}
        legacy = by_key.get((rt.LEGACY_NAMESPACE, rt.LEGACY_KEY))
        if legacy:
            legacy_present += 1
            legacy_ids.add(legacy['value'])
            if not str(legacy['value']).isdigit():
                legacy_non_numeric += 1

        if (rt.LEGACY_NAMESPACE, rt.REGISTERED_AT_KEY) in by_key:
            registered_at_present += 1
            raw = by_key[(rt.LEGACY_NAMESPACE, rt.REGISTERED_AT_KEY)]['value']
            year_counts[str(raw)[:4]] += 1
        else:
            registered_at_absent.append(cand['woo_customer_id'])

        if 'createdAt' in payload or 'created_at' in payload:
            createdat_leaks += 1

    summary = {
        'import_customers': len(imports),
        'legacy_woo_customer_id': {
            'ratified': 'YES - mandatory on every created customer',
            'namespace': rt.LEGACY_NAMESPACE,
            'key': rt.LEGACY_KEY,
            'type': rt.LEGACY_TYPE,
            'source': 'wp_wc_customer_lookup.customer_id',
            'customers_carrying_it': legacy_present,
            'customers_missing_it': len(imports) - legacy_present,
            'distinct_values': len(legacy_ids),
            'values_that_are_not_plain_integers': legacy_non_numeric,
            'unique_across_population': len(legacy_ids) == len(imports),
        },
        'woo_registered_at': {
            'ratified': 'RETAIN as a customer metafield',
            'namespace': rt.LEGACY_NAMESPACE,
            'key': rt.REGISTERED_AT_KEY,
            'type': 'single_line_text_field',
            'source': 'wp_wc_customer_lookup.date_registered',
            'customers_carrying_it': registered_at_present,
            'customers_without_a_source_date': len(registered_at_absent),
            'earliest_year': min(year_counts) if year_counts else None,
            'latest_year': max(year_counts) if year_counts else None,
            'note': (
                'Omitted, never invented, where the source has no date. A guest '
                'checkout row has no registration event to record.'),
        },
        'shopify_createdAt': {
            'written': False,
            'payloads_containing_it': createdat_leaks,
            'note': ('Customer.createdAt is server-controlled and read-only. It is '
                     'never written, manipulated, or approximated.'),
        },
        'reconciliation_chain': 'Woo ID -> custom.legacy_woo_customer_id -> Shopify Customer GID',
    }

    os.makedirs('reports', exist_ok=True)
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {SUMMARY_PATH} - aggregate only, tracked')

    ok = (legacy_present == len(imports)
          and len(legacy_ids) == len(imports)
          and createdat_leaks == 0)
    print('\nlegacy id on every customer, unique, and createdAt untouched:',
          'YES' if ok else 'NO - INVESTIGATE')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
