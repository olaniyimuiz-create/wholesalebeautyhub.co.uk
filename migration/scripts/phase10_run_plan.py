"""Phase 10 bulk run plan under the SIGNED policies. OFFLINE ONLY - creates nothing.

Every earlier report measured the population under a policy that had not been
decided yet. This one measures it under the policies that were actually signed
on 2026-08-22 (ADR-014 Gates 1-5), so the Gate 7 authorization request is made
against the numbers the run will really produce rather than against the
full-population figures that preceded the decisions.

    Gate 1  phone collisions   231 recommendations stand; the 9 contested
                               groups omit the phone for every member
    Gate 2  address policy     A_PLUS - billing, else shipping
    Gate 3  consent            carry FluentCRM `subscribed` forward
    Gate 4  292 missing emails PERMANENT_EXCLUSION (already outside the 12,096)
    Gate 5  247 name conflicts EXCLUDE_AFFECTED_CUSTOMERS -> 11,849 to import

It builds the same stage plans the importer would send and counts them. It has
no transport, it imports the runtime that refuses mutation documents by
construction, and it writes one aggregate file with no PII.

This is a plan, not permission. Gate 7 is unsigned.

Writes reports/phase10_run_plan.json (TRACKED, aggregate only).

Run: python migration/scripts/phase10_run_plan.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_customer_dry_run as dr
import phase10_import_runtime as rt
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows

PLAN_PATH = os.path.join('reports', 'phase10_run_plan.json')

# Measured 2026-08-22 against this store, not assumed. See
# reports/phase10_mutation_cost_analysis.json.
COST_PER_MUTATION = 10
SUSTAINED_MUTATIONS_PER_SECOND = 10


def _refuse(*_args, **_kwargs):
    raise SystemExit('REFUSED: phase10_run_plan must never contact Shopify')


def load_population():
    """IMPORT candidates, plus the Woo IDs caught in a name conflict.

    The conflicted set is read the way phase10_build_test_set reads it: a
    QUARANTINE row for a conflicting identity names the IMPORT row it collides
    with, and that IMPORT row is the one Gate 5 holds back.
    """
    data = load_dump(SQL_DUMP_PATH)
    usermeta, order_billing = data['usermeta'], data['order_billing_by_email']
    staff = {}
    for uid, meta in usermeta.items():
        caps = php_unserialize(meta.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            staff[uid] = set(caps)
    consent = dr.load_fluentcrm_consent()

    seen, imports, conflicted = {}, [], set()
    for _table, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_wc_customer_lookup'}):
        cand = dr.build_candidate(row, usermeta, order_billing)
        classification, reason, notes = dr.classify(cand, seen, staff, consent, set())
        if classification == 'QUARANTINE' and reason == 'duplicate_email_conflicting_identity':
            match = re.search(r'woo_customer_id=(\d+)', notes or '')
            if match:
                conflicted.add(int(match.group(1)))
        if classification in ('IMPORT', 'UPDATE'):
            seen[cand['email']] = cand
            uid = cand['user_id']
            profile_phone = (usermeta.get(uid, {}) or {}).get('billing_phone') if uid else None
            cand['phone_from_profile'] = bool((profile_phone or '').strip())
            imports.append(cand)
    return imports, conflicted, consent


def main():
    dr.graphql_request = _refuse

    print('Reading dump.sql (customer tables only)... offline, no Shopify request.')
    imports, conflicted, consent = load_population()

    # ---------------------------------------------------------------- Gate 5
    run_set = [c for c in imports if c['woo_customer_id'] not in conflicted]

    # ---------------------------------------------------------------- Gate 1
    # Gate 1 signed off "the recommendations stand, the 9 contested groups omit".
    # That is exactly the evidence-scored recommendation with NO reviewer
    # override: KEEP_ONE sends for the scored owner, OMIT_FROM_ALL sends for
    # nobody, and a contested group holds at OMIT_PHONE_PENDING_REVIEW.
    by_key = {}
    for cand in imports:
        if (cand.get('phone') or '').strip():
            by_key.setdefault(rt.phone_canonical(cand.get('phone')), []).append(cand)
    groups = {k: v for k, v in by_key.items() if len(v) > 1}

    phone_action = {}
    group_actions = {rt.ACTION_KEEP_ONE: 0, rt.ACTION_OMIT_FROM_ALL: 0,
                     rt.ACTION_MANUAL_REVIEW: 0}
    for members in groups.values():
        action, owner, _why = rt.recommend_group_action(members)
        group_actions[action] = group_actions.get(action, 0) + 1
        for member in members:
            phone_action[member['woo_customer_id']] = rt.final_phone_action(
                member['woo_customer_id'], action, owner)

    def sends_phone(cand):
        if not (cand.get('phone') or '').strip():
            return False
        return phone_action.get(cand['woo_customer_id'],
                                rt.SEND_PHONE) == rt.SEND_PHONE

    # ---------------------------------------------------------------- Gate 2
    policy = rt.ADDRESS_POLICY_RATIFIED

    creates = addresses = 0
    with_phone = phone_sent = 0
    with_address = 0
    metafields_two = 0
    ineligible = []
    fallback_candidates = 0

    for cand in run_set:
        try:
            stages = rt.plan_customer_import(cand, phone_allowed=sends_phone(cand),
                                             address_policy=policy)
        except rt.NotEligibleForImport as exc:
            ineligible.append(str(exc)[:80])
            continue
        creates += 1
        address_stages = [s for s in stages if s['stage'] == rt.STAGE_ADDRESS]
        addresses += len(address_stages)
        if address_stages:
            with_address += 1
        if len(stages[0]['input']['metafields']) == 2:
            metafields_two += 1
        if (cand.get('phone') or '').strip():
            with_phone += 1
        if 'phone' in stages[0]['input']:
            phone_sent += 1
            # Risk #45: only a phone actually SENT can be rejected, and only
            # then does the fallback cost a second create.
            category, _reason = rt.classify_phone_format(cand.get('phone'))
            if category == rt.PHONE_FORMAT_INVALID or rt.phone_plan_advisory(cand.get('phone')):
                fallback_candidates += 1

    # ---------------------------------------------------------------- Gate 3
    subscribed = sum(1 for c in run_set if consent.get(c['email']) == 'subscribed')

    total = creates + addresses
    worst_case = total + fallback_candidates

    plan = {
        'authorization': 'ADR-014 Gate 7 is UNSIGNED. This is a plan, not permission.',
        'signed_policies': {
            'gate_1_phone': 'recommendations confirmed; 9 contested groups omit',
            'gate_2_address': policy,
            'gate_3_consent': 'carry FluentCRM subscribed forward',
            'gate_4_missing_email': 'PERMANENT_EXCLUSION',
            'gate_5_names': rt.POLICY_EXCLUDE_AFFECTED,
        },
        'phone_collision_groups': {
            'groups': len(groups),
            'customers_affected': sum(len(v) for v in groups.values()),
            'by_recommended_action': group_actions,
        },
        'population': {
            'import_classified': len(imports),
            'held_back_by_gate_5': len(conflicted),
            'to_import': creates,
            'ineligible_at_plan_time': len(ineligible),
        },
        'mutations': {
            'customerCreate': creates,
            'customerAddressCreate': addresses,
            'total': total,
            'phone_fallback_second_creates_expected': fallback_candidates,
            'total_worst_case_with_fallbacks': worst_case,
        },
        'cost': {
            'points_per_mutation_measured': COST_PER_MUTATION,
            'total_points': total * COST_PER_MUTATION,
            'sustained_mutations_per_second': SUSTAINED_MUTATIONS_PER_SECOND,
            'estimated_minutes': round(worst_case / SUSTAINED_MUTATIONS_PER_SECOND / 60, 1),
        },
        'content': {
            'customers_with_an_address': with_address,
            'customers_with_a_phone_in_source': with_phone,
            'phones_actually_sent': phone_sent,
            'phones_omitted_by_gate_1': with_phone - phone_sent,
            'customers_with_both_metafields': metafields_two,
            'customers_with_legacy_id_only': creates - metafields_two,
            'consent_subscribed_in_run': subscribed,
        },
        'notes': {
            'addresses': ('A_PLUS sends at most one address per customer: billing, '
                          'or shipping when there is no billing address.'),
            'phone_fallback': ('Every customer whose phone the offline pre-check flags '
                               'is expected to cost one extra customerCreate, because '
                               'Shopify rejects the whole mutation on a phone error and '
                               'the runtime retries once without the number. The '
                               'customer is never lost. This is an estimate from a '
                               'STRUCTURAL check - Shopify validates against numbering '
                               'plans this project does not hold, so treat it as a '
                               'floor.'),
            'consent': ('Approved but not applied by this plan: consent can be set '
                        'post-import with customerEmailMarketingConsentUpdate.'),
            'not_built': ('No bulk importer exists. phase10_test_import.py is capped at '
                          '10 records. Gate 7 authorizes a run that still has to be '
                          'written and reviewed.'),
        },
    }

    os.makedirs('reports', exist_ok=True)
    with open(PLAN_PATH, 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2, sort_keys=True)

    # No PII may reach this tracked file. Asserted, not assumed.
    blob = json.dumps(plan)
    for cand in run_set[:2000]:
        for value in (cand.get('email'), cand.get('phone'), cand.get('billing_address1')):
            value = (value or '').strip()
            if len(value) >= 6 and value in blob:
                raise SystemExit('ABORTED: PII reached the tracked run plan')

    print(json.dumps(plan, indent=2, sort_keys=True))
    print(f'\nWrote {PLAN_PATH} (aggregate, tracked)')
    print('\nSHOPIFY MUTATIONS: 0 - this is a plan. ADR-014 Gate 7 is unsigned.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
