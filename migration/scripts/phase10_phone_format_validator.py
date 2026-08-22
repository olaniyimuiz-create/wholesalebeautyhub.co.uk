"""Phase 10 offline phone-format pre-check. OFFLINE ONLY - never contacts Shopify.

WHY THIS EXISTS
---------------
The Gate 6 test import lost woo_customer_id=1 outright: Shopify answered
customerCreate with

    userErrors: [{field: ["phone"], message: "Phone is invalid"}]

and created nothing. A phone that fails validation does not cost a phone
number, it costs the whole customer. That is risk #45.

The runtime fallback in phase10_import_runtime now makes that survivable - the
number is dropped, the customer is tagged, the create is retried once. This
script answers the other half of the question: HOW MANY of the 4,450 customers
carrying a phone are affected, known before a bulk run rather than discovered
during one.

WHAT IT CAN AND CANNOT TELL YOU
-------------------------------
It is a structural check - characters, plus sign, digit count - plus one
narrow, clearly-labelled GB numbering-plan advisory. It cannot certify that
Shopify will accept a number, because Shopify validates against national
numbering plans this project has no copy of.

That limit is not theoretical. woo_customer_id=1 is `+44 7...` with a
nine-digit national number where GB mobiles need ten: correctly punctuated,
inside the E.164 length range, and rejected anyway. It passes every generic
check in this file and is caught only by the GB advisory. Read a VALID_E164
count as "nothing structurally wrong", never as "will import".

No number is rewritten. Normalisation is Shopify's job at write time, and it
does it - every GB 07... number in the test cohort came back as +44... and
reconciled clean. NEEDS_NORMALIZATION describes the input; it is not a
prediction of failure.

POPULATION
----------
The IMPORT set, via the committed build_candidate()/classify() from
phase10_customer_dry_run - the same population every other Phase 10 report
uses, not a re-derivation that could drift from it.

OUTPUTS
-------
reports/phase10_phone_validation_summary.json  aggregate counts, TRACKED, no PII
reports/phase10_phone_format_exceptions.csv    per-record detail for a reviewer,
                                               carries real numbers, GITIGNORED
reports/phase10_dropped_phones.jsonl           appended for every flagged number,
                                               same log the live retry writes to,
                                               GITIGNORED

Run: python migration/scripts/phase10_phone_format_validator.py
"""
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_customer_dry_run as dr
import phase10_import_runtime as rt
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows

SUMMARY_PATH = os.path.join('reports', 'phase10_phone_validation_summary.json')
EXCEPTIONS_PATH = os.path.join('reports', 'phase10_phone_format_exceptions.csv')

EXCEPTION_FIELDS = ['woo_customer_id', 'category', 'reason', 'gb_plan_advisory',
                    'digit_count', 'phone', 'expected_import_behaviour']


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_phone_format_validator must never contact Shopify')


def load_import_candidates():
    """The IMPORT set, via the committed classification logic."""
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
        # The store holds 0 customers (verified read-only), so an empty live set
        # keeps this offline without changing any classification.
        classification, _reason, _notes = dr.classify(cand, seen, staff, consent, set())
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand
            imports.append(cand)
    return imports


def expected_behaviour(category, advisory):
    """What the import will actually do with this number, in plain terms.

    Written out per row because the categories alone invite the wrong reading:
    NEEDS_NORMALIZATION sounds like a problem and is not, VALID_E164 sounds
    like a guarantee and is not.
    """
    if category == rt.PHONE_FORMAT_INVALID:
        return ('Shopify will almost certainly reject it; the runtime drops the '
                'phone, tags the customer, and retries once. Customer survives.')
    if advisory:
        return ('Structurally fine but the GB national number is the wrong '
                'length - this is the shape that lost woo 1. Same fallback applies.')
    if category == rt.PHONE_FORMAT_NEEDS_NORMALIZATION:
        return 'Sent as-is; Shopify normalises to E.164. Verified in the Gate 6 run.'
    return 'Sent as-is. No structural problem found - not a guarantee of acceptance.'


def main():
    dr.graphql_request = _refuse  # hard-disable network for this whole run
    run_id = 'phone-precheck-' + datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    print('Reading dump.sql (customer tables only)... offline, no Shopify request.')
    imports = load_import_candidates()

    summary = rt.phone_format_summary(imports)

    rows, flagged_events = [], []
    for cand in sorted(imports, key=lambda c: c['woo_customer_id']):
        raw = (cand.get('phone') or '').strip()
        if not raw:
            continue
        category, reason = rt.classify_phone_format(raw)
        advisory = rt.phone_plan_advisory(raw)
        if category == rt.PHONE_FORMAT_VALID and not advisory:
            continue
        if category == rt.PHONE_FORMAT_NEEDS_NORMALIZATION and not advisory:
            continue
        rows.append({
            'woo_customer_id': cand['woo_customer_id'],
            'category': category,
            'reason': reason,
            'gb_plan_advisory': advisory or '',
            'digit_count': len(rt.phone_digits(raw)),
            'phone': raw,
            'expected_import_behaviour': expected_behaviour(category, advisory),
        })
        flagged_events.append(rt.offline_phone_flag_event(
            cand['woo_customer_id'], raw, advisory or reason, run_id=run_id))

    os.makedirs('reports', exist_ok=True)
    with open(EXCEPTIONS_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EXCEPTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    for event in flagged_events:
        rt.append_dropped_phone(event)

    with_phone = summary['customers_scanned'] - summary['categories'].get(
        rt.PHONE_FORMAT_ABSENT, 0)
    summary.update({
        'run_id': run_id,
        'customers_with_phone': with_phone,
        'customers_without_phone': summary['categories'].get(rt.PHONE_FORMAT_ABSENT, 0),
        'flagged_for_review': len(rows),
        'exceptions_file': EXCEPTIONS_PATH + ' (gitignored - contains real numbers)',
        'dropped_phone_log': rt.DROPPED_PHONES_PATH + ' (gitignored)',
        'validation_limits': (
            'STRUCTURAL ONLY. Characters, plus sign and digit count, plus one GB '
            'numbering-plan advisory. Shopify validates against national numbering '
            'plans this project does not hold, so VALID_E164 means "nothing '
            'structurally wrong", not "Shopify will accept it". woo_customer_id=1 '
            'passes every generic check here and was still rejected live.'),
        'normalization_note': (
            'No value is rewritten. Shopify normalises to E.164 at write time - '
            'every GB 07... number in the Gate 6 cohort came back as +44... and '
            'reconciled clean. NEEDS_NORMALIZATION is not a predicted failure.'),
        'runtime_behaviour': (
            'A phone Shopify rejects costs the phone, not the customer: '
            'phase10_import_runtime.phone_fallback drops the number, tags the '
            'customer ' + rt.TAG_PHONE_DROPPED + ', and the executor retries the '
            'create exactly once. See risk #45.'),
        'shopify_requests': 0,
    })

    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    # No phone number may reach the TRACKED summary. Asserted, not assumed.
    written = open(SUMMARY_PATH, encoding='utf-8').read()
    for cand in imports:
        raw = (cand.get('phone') or '').strip()
        if raw and (raw in written or (len(rt.phone_digits(raw)) > 6
                                       and rt.phone_digits(raw) in written)):
            raise SystemExit('ABORTED: a raw phone number reached the tracked summary')

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f'\nWrote {SUMMARY_PATH} (aggregate, tracked)')
    print(f'Wrote {EXCEPTIONS_PATH} ({len(rows)} row(s), real numbers, gitignored)')
    print(f'Appended {len(flagged_events)} flag(s) to {rt.DROPPED_PHONES_PATH}')

    invalid = summary['categories'].get(rt.PHONE_FORMAT_INVALID, 0)
    advisory = summary['gb_plan_advisory_count']
    print(f'\n{invalid} structurally invalid, {advisory} GB plan advisory, '
          f'{with_phone} customers with a phone.')
    print('Every one of them keeps their customer record: the phone is dropped, '
          'not the person.')
    print('\nSHOPIFY REQUESTS: 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
