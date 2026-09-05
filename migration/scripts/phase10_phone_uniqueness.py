"""Phase 10 phone-uniqueness pre-check. OFFLINE ONLY - never contacts Shopify.

Shopify enforces Customer.phone uniqueness across the whole store, but the
Phase 10 dry run deduplicates on email alone. A phone number shared by two
customers with different emails is therefore completely invisible until the
second customerCreate fails at write time, mid-import, after the first few
thousand records have already been created.

This finds those collisions before anything is written. It reuses the COMMITTED
build_candidate()/classify() from phase10_customer_dry_run so the population is
exactly the IMPORT set, not a re-derivation.

Two outputs:
  reports/phase10_phone_uniqueness.json  - aggregate counts, TRACKED (no PII)
  reports/phase10_phone_collisions.csv   - Woo IDs per collision group,
                                           GITIGNORED (groups customers by a
                                           shared contact number)

No phone number is written to either file, or printed.

Run: python migration/scripts/phase10_phone_uniqueness.py
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

SUMMARY_PATH = os.path.join('reports', 'phase10_phone_uniqueness.json')
COLLISIONS_PATH = os.path.join('reports', 'phase10_phone_collisions.csv')


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_phone_uniqueness must never contact Shopify')


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
        # Live set is empty: the store has 0 customers, verified read-only.
        # Passing an empty set keeps this run offline without changing any
        # classification, since UPDATE requires a live match.
        classification, _reason, _notes = dr.classify(cand, seen, staff, consent, set())
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand
            imports.append(cand)
    return imports


def main():
    dr.graphql_request = _refuse  # hard-disable network for this whole run

    print('Reading dump.sql (customer tables only)...')
    imports = load_import_candidates()

    summary = rt.phone_collision_summary(imports)
    summary['import_customers'] = len(imports)
    summary['customers_without_phone'] = len(imports) - summary['customers_with_phone']
    summary['note'] = (
        'Collisions are detected on a UK-canonical form (0-prefix and +44 treated '
        'as the same subscriber, separators ignored) because Shopify normalises '
        'phone numbers internally. Canonicalisation is for DETECTION ONLY - no '
        'source value is rewritten.')

    with_phone = [c for c in imports if (c.get('phone') or '').strip()]
    groups = rt.phone_collision_groups(with_phone)

    os.makedirs('reports', exist_ok=True)
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    rows = rt.build_collision_report(imports)
    fields = ['collision_group_id', 'normalized_phone_hash', 'affected_customer_count',
              'source_customer_ids', 'registered_accounts_in_group', 'guest_rows_in_group',
              'risk', 'available_decisions', 'recommended_action',
              'chosen_owner_woo_customer_id', 'review_status']
    with open(COLLISIONS_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    high_risk = [r for r in rows if r['risk'] == 'HIGH']
    summary['high_risk_groups'] = len(high_risk)
    summary['customers_in_high_risk_groups'] = sum(r['affected_customer_count'] for r in high_risk)
    summary['unresolved_groups'] = len(rt.unresolved_collision_groups(rows))
    summary['largest_group_registered_accounts'] = (
        max(rows, key=lambda r: r['affected_customer_count'])['registered_accounts_in_group']
        if rows else 0)
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    # No raw phone value may reach either file. Asserted, not assumed.
    written = open(COLLISIONS_PATH, encoding='utf-8').read()
    for cand in with_phone:
        raw = (cand.get('phone') or '').strip()
        if raw and (raw in written or rt.phone_digits(raw) in written):
            raise SystemExit('ABORTED: a raw phone number reached the collision report')

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {SUMMARY_PATH} (aggregate, tracked)')
    print(f'Wrote {COLLISIONS_PATH} ({len(rows)} group(s), hashed labels + Woo IDs, gitignored)')
    if high_risk:
        print(f'\nHIGH RISK: {len(high_risk)} group(s) of {rt.HIGH_RISK_GROUP_SIZE}+ customers '
              f'sharing one number, covering {summary["customers_in_high_risk_groups"]} customers.')
        for r in sorted(high_risk, key=lambda r: -r['affected_customer_count'])[:5]:
            print(f"  group {r['collision_group_id']}: {r['affected_customer_count']} customers "
                  f"({r['registered_accounts_in_group']} registered) -> {r['recommended_action']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
