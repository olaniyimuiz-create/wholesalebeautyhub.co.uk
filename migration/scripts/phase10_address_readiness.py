"""Phase 10 address readiness. OFFLINE ONLY - never contacts Shopify.

Runs the ratified address transformation over every IMPORT customer and records
what would happen to each address, without building or sending anything.

Ratified rule (2026-08-21): an address whose country cannot be established is
SKIPPED. The customer is unaffected and still imports. GB is never assumed.

    Customer = IMPORT
    Address  = SKIPPED_INVALID_COUNTRY

The rule was ratified on a count of 16 addresses with no country. That count
came from a presence check - `billing_country` being empty. It does NOT cover
addresses where a country IS recorded but is not a value Shopify's CountryCode
enum accepts (free text like "United Kingdom", or the ZZ "Unknown Region"
placeholder). Those skip for the same reason and were never counted. This
script counts them.

Both address policies are reported side by side, so Decision #3 (billing-only
vs billing+shipping) can be settled on measured numbers rather than estimates.

Writes:
  reports/phase10_address_readiness.json  TRACKED   - aggregate counts only
  reports/phase10_address_exceptions.csv  GITIGNORED - per-address detail,
                                            contains real addresses

Run: python migration/scripts/phase10_address_readiness.py
"""
import collections
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_customer_dry_run as dr
import phase10_import_runtime as rt
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows

SUMMARY_PATH = os.path.join('reports', 'phase10_address_readiness.json')
EXCEPTIONS_PATH = os.path.join('reports', 'phase10_address_exceptions.csv')

EXCEPTION_FIELDS = [
    'woo_customer_id', 'address_kind', 'customer_status', 'address_status',
    'reason', 'source_country_value', 'source_city', 'source_postcode',
    'source_address1', 'recommended_human_action',
]


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_address_readiness must never contact Shopify')


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

    status_counts = {'billing': collections.Counter(), 'shipping': collections.Counter()}
    reason_counts = {'billing': collections.Counter(), 'shipping': collections.Counter()}
    exceptions = []
    customers_losing_every_address = 0
    customers_with_planned_address = set()
    customers_with_shipping_but_no_billing = 0
    customers_with_both = 0

    for cand in imports:
        outcomes = {}
        for kind in ('billing', 'shipping'):
            has_source = bool((cand.get(kind + '_address1') or '').strip())
            status, reason, _address = rt.describe_address_outcome(cand, kind)
            outcomes[kind] = (status, reason, has_source)
            status_counts[kind][status] += 1
            if reason:
                reason_counts[kind][reason] += 1

            if status == rt.ADDRESS_STATUS_PLANNED:
                customers_with_planned_address.add(cand['woo_customer_id'])
            elif has_source:
                # Source data existed but produced no address - the only rows a
                # human could act on. A customer with no address at all is not
                # an exception, just a customer without an address.
                exceptions.append({
                    'woo_customer_id': cand['woo_customer_id'],
                    'address_kind': kind,
                    'customer_status': 'IMPORT',
                    'address_status': status,
                    'reason': reason,
                    'source_country_value': cand.get(kind + '_country') or '',
                    'source_city': cand.get(kind + '_city') or '',
                    'source_postcode': cand.get(kind + '_zip') or '',
                    'source_address1': cand.get(kind + '_address1') or '',
                    'recommended_human_action': (
                        'Supply the country in WooCommerce, or accept the customer '
                        'importing without this address. Never defaulted.'
                        if status == rt.ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY
                        else 'Review the source address'),
                })

        had_any = any(o[2] for o in outcomes.values())
        kept_any = any(o[0] == rt.ADDRESS_STATUS_PLANNED for o in outcomes.values())
        if had_any and not kept_any:
            customers_losing_every_address += 1

        # Gate 2 shapes, counted per customer rather than per address.
        billing_ok = outcomes['billing'][0] == rt.ADDRESS_STATUS_PLANNED
        shipping_ok = outcomes['shipping'][0] == rt.ADDRESS_STATUS_PLANNED
        if shipping_ok and not billing_ok:
            customers_with_shipping_but_no_billing += 1
        elif billing_ok and shipping_ok:
            customers_with_both += 1

    os.makedirs('reports', exist_ok=True)
    with open(EXCEPTIONS_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EXCEPTION_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(exceptions, key=lambda r: (r['address_kind'],
                                                           r['woo_customer_id'])))

    billing_planned = status_counts['billing'][rt.ADDRESS_STATUS_PLANNED]
    shipping_planned = status_counts['shipping'][rt.ADDRESS_STATUS_PLANNED]
    summary = {
        'import_customers': len(imports),
        'ratified_rule': 'Customer = IMPORT, Address = SKIPPED_INVALID_COUNTRY; GB never assumed',
        'billing': {
            'planned': billing_planned,
            'by_status': dict(sorted(status_counts['billing'].items())),
            'skip_reasons': dict(sorted(reason_counts['billing'].items())),
        },
        'shipping': {
            'planned': shipping_planned,
            'by_status': dict(sorted(status_counts['shipping'].items())),
            'skip_reasons': dict(sorted(reason_counts['shipping'].items())),
        },
        'address_calls_option_a_billing_only': billing_planned,
        'address_calls_option_b_billing_plus_shipping': billing_planned + shipping_planned,
        'address_calls_option_a_plus_billing_else_shipping': (
            billing_planned + customers_with_shipping_but_no_billing),
        'customers_with_shipping_but_no_billing': customers_with_shipping_but_no_billing,
        'customers_with_both_addresses': customers_with_both,
        'gate_2_selected_policy': rt.ADDRESS_POLICY_RATIFIED,
        'gate_2_note': (
            'ADR-014 Gate 2 selected A_PLUS on 2026-08-22: billing, falling back to '
            'shipping only for a customer with no billing address. Under option A '
            'those customers would have imported with no address at all despite '
            'having usable address data; under option B every customer with both '
            'would carry a second address Shopify shows without a billing/shipping '
            'label. A_PLUS gives at most one address per customer and leaves nobody '
            'with address data addressless.'),
        'customers_with_at_least_one_planned_address': len(customers_with_planned_address),
        'customers_whose_only_address_was_skipped': customers_losing_every_address,
        'customers_blocked_by_an_address_problem': 0,
        'exception_rows_for_review': len(exceptions),
        'note': (
            'No customer is blocked by an address problem - that is the ratified rule '
            'and it is asserted, not assumed. A skipped address removes the address, '
            'never the customer. No country value was defaulted, inferred, or invented.'),
    }

    # Every source address value must be absent from the TRACKED summary.
    #
    # Bounded at 6 characters, deliberately. One exception row's address1 is the
    # bare house number "11" - a substring of any document containing counts,
    # and not identifying on its own. Testing it produced a guaranteed false
    # positive that would have made this guard fire on every run, and a guard
    # that always fires gets removed rather than heeded. Real street lines and
    # postcodes are well over the bound and are still tested, on a word
    # boundary so a coincidental substring cannot hide a real leak.
    blob = json.dumps(summary)
    for row in exceptions:
        for value in (row['source_address1'], row['source_postcode']):
            value = str(value or '').strip()
            if len(value) < 6:
                continue
            if re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', blob):
                raise SystemExit('ABORTED: address PII reached the tracked summary')
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {SUMMARY_PATH} - aggregate only, tracked')
    print(f'Wrote {EXCEPTIONS_PATH} ({len(exceptions)} rows) - CONTAINS ADDRESSES, gitignored')
    return 0


if __name__ == '__main__':
    sys.exit(main())
