"""Phase 10 phone collision review dataset. OFFLINE ONLY - never contacts Shopify.

Shopify enforces Customer.phone uniqueness store-wide. 517 IMPORT customers
across 240 groups share a number with someone else, so the phone field cannot
be sent for all of them.

NOTHING IS DELETED OR ALTERED. No phone number in WooCommerce is touched, and
none is rewritten. The only thing this decides is whether a number is SENT to
Shopify for a given customer. A customer whose phone is omitted is still
created in full - they simply have no phone on their Shopify record.

Approved rule (2026-08-21):
  - keep the number only on the customer with the strongest evidence it is
    genuinely their individual number;
  - if it reads as shared, business, or placeholder, omit it from Shopify
    rather than inventing an owner;
  - never attempt to make Shopify accept a duplicate.

Two outputs:

  reports/phase10_phone_collision_review.csv   GITIGNORED
      One row per affected customer, carrying the phone number, email, and
      name a reviewer needs to make the call. Real PII - never tracked.

  reports/phase10_phone_collision_summary.json TRACKED
      Aggregate counts only. No phone number, email, or name. Verified by an
      assertion before the file is written, not by inspection.

Run: python migration/scripts/phase10_phone_collision_review.py
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

REVIEW_PATH = os.path.join('reports', 'phase10_phone_collision_review.csv')
SUMMARY_PATH = os.path.join('reports', 'phase10_phone_collision_summary.json')

REVIEW_FIELDS = [
    'collision_group_id',
    'woo_customer_id',
    'phone_number',
    'customers_sharing_this_number',
    'customer_email',
    'customer_name',
    'is_import',
    'classification',
    'phone_on_own_profile',
    'registered_account',
    'company',
    'ownership_evidence_score',
    'ownership_signals',
    'group_risk',
    'recommended_action',
    'recommended_owner_woo_customer_id',
    'recommendation_rationale',
    'reviewer_decision',
    'reviewer_chosen_owner_woo_customer_id',
    'reviewer_note',
    'final_phone_action',
]


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_phone_collision_review must never contact Shopify')


def load_classified_rows():
    """Every wp_wc_customer_lookup row with its committed classification, plus
    the phone provenance flag the classification pipeline does not surface."""
    data = load_dump(SQL_DUMP_PATH)
    usermeta, order_billing = data['usermeta'], data['order_billing_by_email']

    staff = {}
    for uid, meta in usermeta.items():
        caps = php_unserialize(meta.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            staff[uid] = set(caps)
    consent = dr.load_fluentcrm_consent()

    seen, rows = {}, []
    for _table, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_wc_customer_lookup'}):
        cand = dr.build_candidate(row, usermeta, order_billing)
        classification, _reason, _notes = dr.classify(cand, seen, staff, consent, set())
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand

        # Provenance: is this number on the customer's OWN profile, or was it
        # borrowed from an order's billing block? build_candidate() falls back
        # to the order phone when the customer has no address of their own, and
        # an order phone could belong to whoever placed the order. This is the
        # strongest ownership signal available and it is not otherwise visible.
        uid = cand['user_id']
        profile_phone = (usermeta.get(uid, {}) or {}).get('billing_phone') if uid else None
        cand['phone_from_profile'] = bool((profile_phone or '').strip())
        cand['classification'] = classification
        cand['is_import'] = classification in ('IMPORT', 'UPDATE')
        rows.append(cand)
    return rows


def build_review_rows(all_rows):
    """Group by canonical phone; keep groups where 2+ IMPORT customers collide.

    Only IMPORT rows are ever written to Shopify, so only IMPORT-vs-IMPORT
    sharing can actually break uniqueness. Non-IMPORT rows sharing the same
    number are still listed, flagged is_import=FALSE, because they are useful
    context for a reviewer deciding whose number it is - a quarantined guest
    row using the same number tells you something.
    """
    by_phone = {}
    for cand in all_rows:
        if not (cand.get('phone') or '').strip():
            continue
        key = rt.phone_canonical(cand['phone'])
        if key:
            by_phone.setdefault(key, []).append(cand)

    groups = {}
    for key, members in by_phone.items():
        if sum(1 for m in members if m['is_import']) > 1:
            groups[key] = sorted(members, key=lambda c: c['woo_customer_id'])

    review_rows = []
    group_meta = []
    ordered = sorted(groups, key=lambda k: min(c['woo_customer_id'] for c in groups[k]))
    for group_id, key in enumerate(ordered, 1):
        members = groups[key]
        import_members = [m for m in members if m['is_import']]
        # The recommendation considers only customers who will actually be
        # created; a quarantined row cannot own a Shopify phone field.
        action, owner, rationale = rt.recommend_group_action(import_members)
        risk = 'HIGH' if len(import_members) >= rt.HIGH_RISK_GROUP_SIZE else 'NORMAL'
        group_meta.append({
            'collision_group_id': group_id,
            'import_members': len(import_members),
            'total_members': len(members),
            'action': action,
            'owner': owner,
            'risk': risk,
        })

        for m in members:
            score, signals = rt.phone_evidence(m)
            name = ' '.join(p for p in (m.get('first_name'), m.get('last_name')) if p).strip()
            review_rows.append({
                'collision_group_id': group_id,
                'woo_customer_id': m['woo_customer_id'],
                'phone_number': m['phone'],
                'customers_sharing_this_number': len(import_members),
                'customer_email': m['email'],
                'customer_name': name,
                'is_import': 'TRUE' if m['is_import'] else 'FALSE',
                'classification': m['classification'],
                'phone_on_own_profile': 'TRUE' if m['phone_from_profile'] else 'FALSE',
                'registered_account': 'TRUE' if m['is_registered'] else 'FALSE',
                'company': m.get('company') or '',
                'ownership_evidence_score': score if m['is_import'] else '',
                'ownership_signals': '+'.join(signals),
                'group_risk': risk,
                'recommended_action': action,
                'recommended_owner_woo_customer_id': owner if owner is not None else '',
                'recommendation_rationale': rationale,
                # Reviewer columns start empty. The recommendation is a starting
                # point, not a decision already taken on their behalf.
                'reviewer_decision': '',
                'reviewer_chosen_owner_woo_customer_id': '',
                'reviewer_note': '',
                'final_phone_action': (
                    rt.final_phone_action(m['woo_customer_id'], action, owner)
                    if m['is_import'] else 'NOT_IMPORTED'),
            })
    return review_rows, group_meta


def main():
    dr.graphql_request = _refuse

    print('Reading dump.sql (customer tables only)...')
    all_rows = load_classified_rows()
    review_rows, group_meta = build_review_rows(all_rows)

    os.makedirs('reports', exist_ok=True)
    with open(REVIEW_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(review_rows)

    import_rows = [r for r in review_rows if r['is_import'] == 'TRUE']
    by_action = {}
    for g in group_meta:
        by_action[g['action']] = by_action.get(g['action'], 0) + 1
    send = sum(1 for r in import_rows if r['final_phone_action'] == rt.SEND_PHONE)
    omit = sum(1 for r in import_rows if r['final_phone_action'] == rt.OMIT_PHONE)
    hold = sum(1 for r in import_rows if r['final_phone_action'] == rt.HOLD_PENDING_REVIEW)

    summary = {
        'collision_groups': len(group_meta),
        'import_customers_affected': len(import_rows),
        'non_import_rows_listed_as_context': len(review_rows) - len(import_rows),
        'largest_group': max((g['import_members'] for g in group_meta), default=0),
        'high_risk_groups': sum(1 for g in group_meta if g['risk'] == 'HIGH'),
        'groups_by_recommended_action': by_action,
        'customers_recommended_send_phone': send,
        'customers_recommended_omit_phone': omit,
        'customers_held_pending_manual_review': hold,
        'reviewer_decisions_recorded': 0,
        'note': (
            'Recommendations only - every reviewer_decision column is empty. No phone '
            'number was deleted or altered in the source; the only question here is '
            'whether a number is SENT to Shopify. A customer whose phone is omitted is '
            'still created in full.'),
    }

    # The tracked file must not contain a phone, email, or name. Asserted.
    blob = json.dumps(summary)
    for r in review_rows:
        for value in (r['phone_number'], r['customer_email'], r['customer_name']):
            if value and str(value) in blob:
                raise SystemExit('ABORTED: PII reached the tracked summary')
        if rt.phone_digits(r['phone_number']) and rt.phone_digits(r['phone_number']) in blob:
            raise SystemExit('ABORTED: a phone number reached the tracked summary')
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {REVIEW_PATH} ({len(review_rows)} rows) - CONTAINS PII, gitignored')
    print(f'Wrote {SUMMARY_PATH} - aggregate only, tracked')

    high = [g for g in group_meta if g['risk'] == 'HIGH']
    if high:
        print(f'\nHIGH RISK ({len(high)} group(s)):')
        for g in sorted(high, key=lambda g: -g['import_members']):
            print(f"  group {g['collision_group_id']}: {g['import_members']} customers "
                  f"-> {g['action']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
