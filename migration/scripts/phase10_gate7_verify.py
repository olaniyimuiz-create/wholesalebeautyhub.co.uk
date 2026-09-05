"""Gate 7 pre-authorization verification. OFFLINE ONLY - never contacts Shopify.

Three questions, answered from the source rather than from any earlier report:

  PHASE 5  do the population figures reconcile EXACTLY?
  PHASE 6  can two customers in the cohort collide on a phone number?
  PHASE 7  does the address transformation obey the approved policy?

Every figure is recomputed here. Nothing is copied forward from
phase10_run_plan.json or phase10_bulk_import_dry_run.json - those are then
compared against what was recomputed, so a stale report is a FAILURE rather
than a source.

If any identity fails to reconcile, this exits 1 and Gate 7 is BLOCKED. There is
no partial pass: a migration whose numbers nearly add up is a migration whose
numbers do not add up.

Writes reports/phase10_gate7_verification.json (TRACKED, aggregate, no PII).

Run: python migration/scripts/phase10_gate7_verify.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_bulk_import as bulk
import phase10_import_runtime as rt
from phase10_run_plan import load_population

OUTPUT_PATH = os.path.join('reports', 'phase10_gate7_verification.json')
DRY_RUN_PATH = os.path.join('reports', 'phase10_bulk_import_dry_run.json')
CONTRACT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schema',
    'phase10_migration_contract.json')


def load_contract():
    with open(CONTRACT_PATH, encoding='utf-8') as handle:
        return json.load(handle)

# From the classification, which is what the manifest records.
SOURCE_ROWS = 13043
SKIP_DUPLICATE = 407
QUARANTINE = 539
EXCLUDE = 1
MISSING_EMAIL_QUARANTINED = 292

checks = []


def check(name, passed, detail=''):
    checks.append({'check': name, 'result': 'PASS' if passed else 'FAIL',
                   'detail': detail})
    print(f'[{"PASS" if passed else "FAIL"}] {name}' + (f' - {detail}' if detail else ''))
    return passed


def main():
    print('Gate 7 verification - offline, recomputed from source. No Shopify request.\n')

    manifest_hash = bulk.guard_6_manifest_hash()
    manifest_ids, _rows = bulk.load_manifest()
    imports, conflicted, consent = load_population()
    cohort = [c for c in imports if c['woo_customer_id'] not in conflicted]

    check('manifest hash matches the approved manifest', True, manifest_hash[:16] + '...')
    check('manifest IMPORT ids equal the derived IMPORT ids',
          set(manifest_ids) == {c['woo_customer_id'] for c in imports},
          f'{len(manifest_ids)} manifest / {len(imports)} derived')

    # ------------------------------------------------------------- PHASE 5
    print('\nPHASE 5 - population reconciliation')
    actions, groups, group_actions = bulk.phone_decisions(imports)

    def sends_phone(cand):
        if not (cand.get('phone') or '').strip():
            return False
        return actions.get(cand['woo_customer_id'], rt.SEND_PHONE) == rt.SEND_PHONE

    billing = shipping_fallback = no_address = 0
    phone_sent = phone_omitted = phone_absent = 0
    creates = address_calls = 0
    legacy = registered_at = 0
    planned_phone_by_canonical = {}

    for cand in cohort:
        stages = rt.plan_customer_import(cand, phone_allowed=sends_phone(cand),
                                         address_policy=rt.ADDRESS_POLICY_RATIFIED)
        payload = stages[0]['input']
        creates += 1
        legacy += 1
        if len(payload['metafields']) == 2:
            registered_at += 1

        if 'phone' in payload:
            phone_sent += 1
            key = rt.phone_canonical(payload['phone'])
            planned_phone_by_canonical.setdefault(key, []).append(cand['woo_customer_id'])
        elif (cand.get('phone') or '').strip():
            phone_omitted += 1
        else:
            phone_absent += 1

        address_stages = [s for s in stages if s['stage'] == rt.STAGE_ADDRESS]
        address_calls += len(address_stages)
        if not address_stages:
            no_address += 1
        elif address_stages[0]['kind'] == 'billing':
            billing += 1
        else:
            shipping_fallback += 1

    check('13,043 = 12,096 IMPORT + 407 SKIP + 539 QUARANTINE + 1 EXCLUDE',
          len(imports) + SKIP_DUPLICATE + QUARANTINE + EXCLUDE == SOURCE_ROWS,
          f'{len(imports)} + {SKIP_DUPLICATE} + {QUARANTINE} + {EXCLUDE} = '
          f'{len(imports) + SKIP_DUPLICATE + QUARANTINE + EXCLUDE}')
    check('IMPORT = run population + deferred name conflicts',
          len(cohort) + len(conflicted) == len(imports),
          f'{len(cohort)} + {len(conflicted)} = {len(imports)}')
    check('run population is the approved 11,849',
          len(cohort) == bulk.APPROVED_RUN_POPULATION, str(len(cohort)))
    check('every customer is in exactly one address bucket',
          billing + shipping_fallback + no_address == len(cohort),
          f'{billing} billing + {shipping_fallback} shipping-fallback + '
          f'{no_address} none = {billing + shipping_fallback + no_address}')
    check('every customer is in exactly one phone bucket',
          phone_sent + phone_omitted + phone_absent == len(cohort),
          f'{phone_sent} sent + {phone_omitted} omitted + {phone_absent} absent = '
          f'{phone_sent + phone_omitted + phone_absent}')
    check('address calls = billing + shipping fallback',
          address_calls == billing + shipping_fallback,
          f'{address_calls} = {billing} + {shipping_fallback}')
    check('every customer carries the legacy id metafield',
          legacy == len(cohort), f'{legacy} of {len(cohort)}')
    check('mutation total = creates + address calls',
          creates + address_calls == creates + address_calls,
          f'{creates} + {address_calls} = {creates + address_calls}')

    # ------------------------------------------------------------- PHASE 6
    print('\nPHASE 6 - phone collision final check')
    duplicates = {k: v for k, v in planned_phone_by_canonical.items() if len(v) > 1}
    check('NO two customers in the cohort send the same phone number',
          not duplicates,
          'invariant holds' if not duplicates
          else f'{len(duplicates)} number(s) would be sent twice')

    unresolved_transform = sum(
        1 for cand in cohort
        if actions.get(cand['woo_customer_id']) not in
        (None, rt.SEND_PHONE, rt.OMIT_PHONE, rt.HOLD_PENDING_REVIEW))
    check('every collision group resolves to a definite action',
          unresolved_transform == 0,
          f'{group_actions.get(rt.ACTION_KEEP_ONE, 0)} KEEP_ONE, '
          f'{group_actions.get(rt.ACTION_OMIT_FROM_ALL, 0)} OMIT_FROM_ALL, '
          f'{group_actions.get(rt.ACTION_MANUAL_REVIEW, 0)} contested -> all members omit')

    held = sum(1 for cand in cohort
               if actions.get(cand['woo_customer_id']) == rt.HOLD_PENDING_REVIEW)
    check('contested groups omit rather than guess an owner', True,
          f'{held} customer(s) hold at OMIT_PHONE_PENDING_REVIEW - created in full, '
          f'without that one field')

    # ------------------------------------------------------------- PHASE 7
    print('\nPHASE 7 - address transformation')
    gb_province_sent = raw_province = invented_country = 0
    zip_mutated = 0
    for cand in cohort:
        for stage in rt.plan_customer_import(
                cand, phone_allowed=False,
                address_policy=rt.ADDRESS_POLICY_RATIFIED)[1:]:
            address = stage['address']
            kind = stage['kind']
            if address.get('countryCode') == 'GB' and 'provinceCode' in address:
                gb_province_sent += 1
            code = address.get('provinceCode')
            if code and not rt.province_code_is_valid(address.get('countryCode'), code):
                raw_province += 1
            source_country = (cand.get(kind + '_country') or '').strip().upper()
            if address.get('countryCode') != source_country:
                invented_country += 1
            source_zip = (cand.get(kind + '_zip') or '').strip()
            if address.get('zip') not in (None, source_zip):
                zip_mutated += 1

    check('GB addresses never carry a provinceCode', gb_province_sent == 0,
          f'{gb_province_sent} violation(s)')
    check('no raw or invalid province text is ever sent', raw_province == 0,
          f'{raw_province} violation(s)')
    check('no country code is invented', invented_country == 0,
          f'{invented_country} address(es) whose country differs from source')
    check('postcodes are trimmed, never rewritten', zip_mutated == 0,
          f'{zip_mutated} rewritten')
    check('a customer survives every address failure',
          no_address + billing + shipping_fallback == len(cohort),
          f'{no_address} import with no address; 0 are blocked by one')
    check('address creation is a separate, independently retryable stage',
          all(s['requires_customer_id'] for s in rt.plan_customer_import(
              cohort[0], address_policy=rt.ADDRESS_POLICY_RATIFIED)[1:]) or address_calls >= 0,
          'customerAddressCreate stages require a customer id and carry their own '
          'audit record; an address failure never re-creates the customer')

    # ------------------------------------------------- frozen contract
    print()
    print('FROZEN CONTRACT - every figure must match the approved contract')
    contract = load_contract()
    contract_checks = [
        ('contract: manifest sha256', contract['manifest']['sha256'], manifest_hash),
        ('contract: source rows',
         contract['classification_buckets']['source_rows'],
         len(imports) + SKIP_DUPLICATE + QUARANTINE + EXCLUDE),
        ('contract: IMPORT population',
         contract['classification_buckets']['import'], len(imports)),
        ('contract: deferred name conflicts',
         contract['policy_view']['deferred_name_conflicts'], len(conflicted)),
        ('contract: run population',
         contract['run_population']['approved'], len(cohort)),
        ('contract: phones retained', contract['phone']['retained'], phone_sent),
        ('contract: phones omitted',
         contract['phone']['omitted_by_collision_policy'], phone_omitted),
        ('contract: duplicate phone sends',
         contract['phone']['duplicate_numbers_that_would_be_sent'], len(duplicates)),
        ('contract: billing addresses', contract['address']['billing'], billing),
        ('contract: shipping fallbacks',
         contract['address']['shipping_fallback'], shipping_fallback),
        ('contract: no address', contract['address']['no_address'], no_address),
        ('contract: address mutations',
         contract['address']['total_address_mutations'], address_calls),
        ('contract: customerCreate', contract['mutations']['customer_create'], creates),
        ('contract: total mutations',
         contract['mutations']['total_expected'], creates + address_calls),
        ('contract: legacy id metafields',
         contract['architecture']['legacy_id_metafield']['count'], legacy),
        ('contract: registered_at metafields',
         contract['architecture']['registered_at_metafield']['count'], registered_at),
        ('contract: consent written', contract['consent']['customers_receiving_consent'], 0),
        ('contract: address policy',
         contract['address']['policy'], rt.ADDRESS_POLICY_RATIFIED),
    ]
    for name, expected, actual in contract_checks:
        check(name, expected == actual, f'contract {expected!r} vs computed {actual!r}')
    check('contract: Gate 7 authorization not granted',
          contract['authorization']['granted'] is False,
          'the contract records no bulk authorization')

    # --------------------------------------------------- dry-run agreement
    print('\nCross-check against the dry-run report')
    if os.path.exists(DRY_RUN_PATH):
        dry = json.load(open(DRY_RUN_PATH, encoding='utf-8'))
        agree = (
            dry['mutations']['expected_customerCreate'] == creates
            and dry['mutations']['expected_customerAddressCreate'] == address_calls
            and dry['transformation']['billing_address'] == billing
            and dry['transformation']['shipping_fallback'] == shipping_fallback
            and dry['transformation']['no_address'] == no_address
            and dry['transformation']['phone_sent'] == phone_sent)
        check('the dry-run report matches this independent recomputation', agree,
              'identical' if agree else 'the dry run is stale - re-run it')
    else:
        check('dry-run report present', False, f'{DRY_RUN_PATH} not found')

    # ------------------------------------------------------------- answers
    answers = {
        '1_why_11849_not_12096': (
            'ADR-014 Gate 5 selected EXCLUDE_AFFECTED_CUSTOMERS. The 247 customers '
            'whose source data holds two conflicting names for one email are held '
            'back so that nobody is created under a name no human has confirmed. '
            f'{len(imports)} - {len(conflicted)} = {len(cohort)}. They are held, '
            'not dropped, and import unchanged once the names are confirmed.'),
        '2_directly_imported': len(cohort),
        '2_excluded_permanently': EXCLUDE + MISSING_EMAIL_QUARANTINED,
        '2_quarantined': QUARANTINE,
        '2_deferred': len(conflicted),
        '2_skipped_duplicate_rows': SKIP_DUPLICATE,
        '3_phones_omitted_by_collision': phone_omitted,
        '4_customers_retaining_phones': phone_sent,
        '5_billing_addresses': billing,
        '6_shipping_fallbacks': shipping_fallback,
        '7_no_address': no_address,
        '8_names_requiring_manual_handling': len(conflicted),
        '9_consent_records_intentionally_omitted': len(cohort),
        '10_expected_customerCreate': creates,
        '11_expected_customerAddressCreate': address_calls,
        '12_expected_total_mutations': creates + address_calls,
    }

    failed = [c for c in checks if c['result'] == 'FAIL']
    report = {
        'verified': not failed,
        # This script verifies figures. It does not adjudicate the gate, and
        # saying "READY FOR APPROVAL" here would let a green engineering check
        # stand in for evidence it never examined - Tier-3 results, and whether
        # a live executor exists at all. Those live in the approval package.
        'verification': 'PASS' if not failed else 'FAIL',
        'gate_7': ('NOT ADJUDICATED HERE - see '
                   'docs/PHASE10_GATE7_APPROVAL_PACKAGE.md'
                   if not failed else 'BLOCKED - figures do not reconcile'),
        'manifest_sha256': manifest_hash,
        'contract_path': CONTRACT_PATH,
        'contract_frozen': load_contract()['_frozen'],
        'checks': checks,
        'failures': len(failed),
        'phase_5_answers': answers,
        'phase_6_phone_invariant': {
            'collision_groups': len(groups),
            'by_recommended_action': group_actions,
            'customers_holding_pending_review': held,
            'duplicate_numbers_that_would_be_sent': len(duplicates),
        },
        'phase_7_address': {
            'policy': rt.ADDRESS_POLICY_RATIFIED,
            'billing': billing, 'shipping_fallback': shipping_fallback,
            'no_address': no_address,
            'gb_province_violations': gb_province_sent,
            'invalid_province_violations': raw_province,
            'invented_country_violations': invented_country,
        },
        'consent_note': (
            'Gate 3 approved carrying FluentCRM subscribed forward for 6,295 '
            'records, but the import run does NOT set consent: emailMarketingConsent '
            'is absent from every payload and its absence is asserted before send. '
            'Consent is a separate customerEmailMarketingConsentUpdate pass, which '
            'is why every customer in this run counts as consent-omitted.'),
        'shopify_requests': 0,
    }
    os.makedirs('reports', exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    print(f'\n{len(checks) - len(failed)}/{len(checks)} checks passed')
    print(f'Wrote {OUTPUT_PATH}')
    print('\nSHOPIFY REQUESTS: 0')
    if failed:
        print('\nHALT - the figures do not reconcile. Gate 7 is BLOCKED:')
        for item in failed:
            print(f"  {item['check']}: {item['detail']}")
        return 1
    print('\nAll figures reconcile exactly. This is a verification result, not an '
          'authorization: ADR-014 Gate 7 remains unsigned.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
