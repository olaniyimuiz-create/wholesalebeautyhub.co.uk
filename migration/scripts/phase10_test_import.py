"""Phase 10 Step 10 - authorized 10-customer test import, reconcile, then delete.

AUTHORIZED: ADR-014 Gate 6, 2026-08-22, project/store owner.
SCOPE: exactly the 10 customers in reports/phase10_test_import_set.csv, into
wholesale-beautyhub.myshopify.com. Create, reconcile, delete. Nothing else.

THIS SCRIPT WRITES TO SHOPIFY. It is the first thing in Phase 10 that does.

Guards, all of which abort rather than warn:
  * hard cap of 10 records - a longer cohort file is refused outright
  * store must hold 0 customers at start; anything else halts
  * every payload must carry custom.legacy_woo_customer_id, asserted before send
  * no password field anywhere (NEW_CUSTOMER_ACCOUNTS; the field does not exist)
  * consent omitted for all 10 - Gate 3's approval governs the full import, not
    this test, and this script has no code path that sets it
  * 401 / ACCESS_DENIED halts immediately, no retry
  * delete phase only touches GIDs this run created AND whose legacy metafield
    matches the cohort - it cannot delete anything else
  * idempotency: a Woo id already present in the live legacy map is SKIPPED,
    never created twice
  * a phone rejected by Shopify costs the phone, never the customer - the
    number is dropped, the customer is tagged, and the create is retried once
    (risk #45, added after this run lost woo_customer_id=1 to "Phone is invalid")

phase10_import_runtime is used for throttling, backoff, and transformation, but
NOT for transport: it refuses mutation documents by construction and keeps that
guarantee. Mutations are sent from here.

Outputs:
  reports/phase10_test_import_log.jsonl          append-only ledger (gitignored)
  reports/phase10_test_import_reconciliation.csv filled template (gitignored)
  reports/phase10_dropped_phones.jsonl           dropped numbers (gitignored)
  reports/phase10_test_import_result.json        aggregate counts (tracked)

Run: python migration/scripts/phase10_test_import.py --execute
"""
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt
import phase10_province_validator as pv
from phase9_preflight import get_config, graphql_request

TEST_SET_PATH = os.path.join('reports', 'phase10_test_import_set.csv')
LEDGER_PATH = os.path.join('reports', 'phase10_test_import_log.jsonl')
RECON_PATH = os.path.join('reports', 'phase10_test_import_reconciliation.csv')
RESULT_PATH = os.path.join('reports', 'phase10_test_import_result.json')

MAX_COHORT = 10
AUTHORIZATION = 'ADR-014 Gate 6, 2026-08-22, project/store owner'

CUSTOMER_CREATE = '''mutation testCustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id email firstName lastName phone tags
               metafield(namespace: "custom", key: "legacy_woo_customer_id") { value } }
    userErrors { field message }
  }
}'''

ADDRESS_CREATE = '''mutation testAddressCreate($customerId: ID!, $address: MailingAddressInput!, $setAsDefault: Boolean) {
  customerAddressCreate(customerId: $customerId, address: $address, setAsDefault: $setAsDefault) {
    address { id address1 city zip countryCodeV2 provinceCode company }
    userErrors { field message }
  }
}'''

CUSTOMER_DELETE = '''mutation testCustomerDelete($id: ID!) {
  customerDelete(input: {id: $id}) { deletedCustomerId userErrors { field message } }
}'''


class Halt(RuntimeError):
    """Abort. Nothing further is sent."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(entry):
    os.makedirs('reports', exist_ok=True)
    with open(LEDGER_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def send(domain, token, api_version, document, variables, throttle):
    """One mutation. Halts on auth failure; never retries a 401."""
    try:
        response = graphql_request(domain, token, api_version, document, variables)
    except Exception as exc:  # noqa: BLE001
        text = f'{type(exc).__name__}: {exc}'
        if '401' in text or 'Unauthorized' in text or 'Invalid API key' in text:
            raise Halt(f'authentication failed mid-run: {rt.sanitize(text)[:120]}') from None
        raise
    if 'errors' in response:
        codes = {(e.get('extensions') or {}).get('code') for e in response['errors']}
        if 'ACCESS_DENIED' in codes or 'UNAUTHENTICATED' in codes:
            raise Halt(f'access denied mid-run: {sorted(c for c in codes if c)}')
        if 'THROTTLED' in codes:
            raise Halt('throttled - unexpected at 17 mutations; investigate before retrying')
        raise Halt(f'GraphQL error: {rt.sanitize(json.dumps(response["errors"]))[:200]}')
    throttle.observe(response)
    return response


def assert_payload_safe(payload, woo_id):
    """Every guard that must hold before a create is sent."""
    rt.assert_legacy_metafield_present(payload)
    rt.assert_no_server_controlled_fields(payload)
    if 'password' in payload:
        raise Halt(f'woo_customer_id={woo_id}: payload carries a password field')
    if 'emailMarketingConsent' in payload:
        raise Halt(f'woo_customer_id={woo_id}: Gate 6 authorized consent OMITTED for '
                   f'this test; refusing to send it')
    if 'addresses' in payload:
        raise Halt(f'woo_customer_id={woo_id}: CustomerInput has no addresses field')
    return True


def create_customer(send_mutation, payload, woo_id, run_id,
                    log_path=rt.DROPPED_PHONES_PATH):
    """customerCreate, with the risk #45 retry-without-phone fallback.

    Shopify rejects the whole mutation on a phone validation error, so without
    this a bad phone number costs the entire customer. woo_customer_id=1 was
    lost exactly this way in the Gate 6 run. On a phone userError the number is
    dropped, the customer is tagged so the loss is visible in Shopify, the
    original is written to the dropped-phone log, and the create is re-issued
    ONCE. Nothing else about the payload changes - the legacy metafield rides
    through unchanged, which is asserted, not assumed.

    send_mutation is callable(document, variables) -> response, supplied by the
    caller. Transport is injected so the fallback can be tested offline against
    a canned userError instead of only in a live run.

    Returns (result, user_errors, attempts, fallback_event). fallback_event is
    None when no retry was needed.
    """
    resp = send_mutation(CUSTOMER_CREATE, {'input': payload})
    result = resp['data']['customerCreate']
    errs = result.get('userErrors') or []
    if result.get('customer') and not errs:
        return result, errs, 1, None
    if not (rt.is_phone_user_error(errs) and 'phone' in payload):
        return result, errs, 1, None

    fallback = rt.phone_fallback(payload, errs, woo_id, operation='customerCreate',
                                 run_id=run_id, log_path=log_path)
    retry_payload = fallback['input']
    # The retry is a fresh payload and gets the same scrutiny as the first one.
    assert_payload_safe(retry_payload, woo_id)
    resp = send_mutation(CUSTOMER_CREATE, {'input': retry_payload})
    result = resp['data']['customerCreate']
    return result, result.get('userErrors') or [], 2, fallback['event']


def load_cohort():
    if not os.path.exists(TEST_SET_PATH):
        raise Halt(f'{TEST_SET_PATH} not found - run phase10_build_test_set.py first')
    rows = list(csv.DictReader(open(TEST_SET_PATH, encoding='utf-8')))
    if len(rows) > MAX_COHORT:
        raise Halt(f'cohort has {len(rows)} rows; Gate 6 authorized {MAX_COHORT}. Refusing.')
    return rows


def candidate_from_row(row):
    """Rebuild the transformation input from the cohort file."""
    return {
        'woo_customer_id': int(row['woo_customer_id']),
        'email': row['email'], 'email_raw': row['email'],
        'first_name': row['first_name'], 'last_name': row['last_name'],
        'company': row['company'], 'phone': row['phone'],
        'is_registered': row['is_registered'] == 'True',
        'date_registered': row['date_registered'],
        'billing_address1': row['billing_address1'], 'billing_address2': '',
        'billing_city': row['billing_city'], 'billing_province': row['billing_province'],
        'billing_zip': row['billing_zip'], 'billing_country': row['billing_country'],
        'has_shipping_address': row['has_shipping_address'] == 'True',
        'shipping_address1': '',
    }


def main():
    if '--execute' not in sys.argv:
        print(__doc__)
        print('Refusing to run without --execute. Nothing was sent.')
        return 2

    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or '2025-01'
    run_id = f'test-import-{now()}'
    throttle = rt.ThrottleController()

    print(f'AUTHORIZATION: {AUTHORIZATION}')
    print('Scope: 10 customers, create -> reconcile -> delete.\n')

    if not domain or not token:
        print('NOT_CONFIGURED - nothing sent.')
        return 2

    # ---------------------------------------------------------- pre-flight
    print('Pre-flight (read-only)...')
    pre = graphql_request(domain, token, api_version,
                          '{ customersCount { count } currentAppInstallation '
                          '{ accessScopes { handle } } }')
    if 'errors' in pre:
        print(f'HALT: pre-flight {json.dumps(pre["errors"])[:160]}')
        return 1
    scopes = {s['handle'] for s in pre['data']['currentAppInstallation']['accessScopes']}
    before = pre['data']['customersCount']['count']
    if 'write_customers' not in scopes:
        print('HALT: write_customers not granted.')
        return 1
    if before != 0:
        print(f'HALT: store holds {before} customer(s); Gate 6 assumed an empty store. '
              f'Investigate before writing.')
        return 1
    print(f'  {len(scopes)} scopes, {before} customers before. Proceeding.\n')

    cohort = load_cohort()
    print(f'Cohort: {len(cohort)} customer(s) from {TEST_SET_PATH}')

    # Idempotency: anything already live is skipped, never re-created.
    def graphql_send(document, variables=None):
        return graphql_request(domain, token, api_version, document, variables)

    legacy_map, pages = rt.fetch_existing_legacy_map(graphql_send)
    print(f'  legacy-id map: {len(legacy_map)} existing ({pages} page scan)\n')

    created, skipped, failed = [], [], []
    phone_fallbacks = []
    mutations = {'customerCreate': 0, 'customerAddressCreate': 0, 'customerDelete': 0}

    def mutate(document, variables, counter):
        """Pace, send, count. The retry payload is a mutation like any other and
        is counted like one - a fallback costs a second call and the cost report
        must say so."""
        throttle.pace()
        response = send(domain, token, api_version, document, variables, throttle)
        mutations[counter] += 1
        return response

    try:
        # ------------------------------------------------------ create phase
        for row in cohort:
            woo_id = int(row['woo_customer_id'])
            if str(woo_id) in legacy_map:
                skipped.append(woo_id)
                log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                     'operation': 'customerCreate', 'status': 'SKIPPED_ALREADY_PRESENT',
                     'shopify_customer_gid': legacy_map[str(woo_id)]['gid']})
                print(f'  woo {woo_id}: already live, skipped')
                continue

            cand = candidate_from_row(row)
            phone_allowed = row['phone_action'] == 'SEND_PHONE'
            plan = rt.plan_customer_import(cand, include_shipping=False,
                                           phone_allowed=phone_allowed)
            payload = plan[0]['input']
            assert_payload_safe(payload, woo_id)

            result, errs, attempts, fallback = create_customer(
                lambda doc, variables: mutate(doc, variables, 'customerCreate'),
                payload, woo_id, run_id)
            if fallback is not None:
                phone_fallbacks.append(woo_id)
                log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                     'operation': 'customerCreate', 'status': 'PHONE_DROPPED_RETRYING',
                     'reason': fallback['reason'],
                     'user_errors': fallback['user_errors']})
                print(f'  woo {woo_id}: phone rejected ({fallback["reason"]}) - '
                      f'dropping the number and retrying once')
            if errs or not result.get('customer'):
                failed.append(woo_id)
                log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                     'operation': 'customerCreate', 'status': 'FAILED',
                     'attempts': attempts,
                     'phone_fallback_attempted': fallback is not None,
                     'user_errors': rt.sanitize_user_errors(errs)})
                print(f'  woo {woo_id}: FAILED - {[e.get("field") for e in errs]}')
                continue

            gid = result['customer']['id']
            legacy_live = (result['customer'].get('metafield') or {}).get('value')
            if legacy_live != str(woo_id):
                raise Halt(f'woo {woo_id}: created customer {gid} carries legacy id '
                           f'{legacy_live!r}; the identity chain is broken')
            # The payload actually accepted, which is not the planned payload
            # when the phone was dropped. Reconciliation must compare against
            # what was sent, or a successful fallback reports as a mismatch.
            sent_payload = rt.strip_phone_for_retry(payload) if fallback else payload
            created.append({'woo_customer_id': woo_id, 'gid': gid, 'row': row,
                            'plan': plan, 'live': result['customer'], 'addresses': [],
                            'sent_payload': sent_payload,
                            'phone_dropped': fallback is not None})
            log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                 'operation': 'customerCreate', 'status': 'CREATED',
                 'attempts': attempts, 'phone_dropped': fallback is not None,
                 'shopify_customer_gid': gid, 'legacy_id_confirmed': True})
            print(f'  woo {woo_id}: created {gid.rsplit("/", 1)[-1]}, legacy id confirmed')

            for stage in plan[1:]:
                throttle.pace()
                aresp = send(domain, token, api_version, ADDRESS_CREATE,
                             {'customerId': gid, 'address': stage['address'],
                              'setAsDefault': stage['setAsDefault']}, throttle)
                mutations['customerAddressCreate'] += 1
                ares = aresp['data']['customerAddressCreate']
                aerrs = ares.get('userErrors') or []
                if aerrs or not ares.get('address'):
                    log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                         'operation': 'customerAddressCreate', 'status': 'FAILED',
                         'shopify_customer_gid': gid,
                         'user_errors': rt.sanitize_user_errors(aerrs)})
                    print(f'    address FAILED - {[e.get("field") for e in aerrs]}')
                else:
                    created[-1]['addresses'].append(ares['address'])
                    log({'ts': now(), 'run_id': run_id, 'woo_customer_id': woo_id,
                         'operation': 'customerAddressCreate', 'status': 'CREATED',
                         'shopify_customer_gid': gid})
                    print('    address created')

        # ------------------------------------------------- reconciliation
        print(f'\nReconciling {len(created)} customer(s) against the template...')
        recon = []
        for item in created:
            row, live, plan = item['row'], item['live'], item['plan']
            expected_payload = item['sent_payload']
            addr = item['addresses'][0] if item['addresses'] else {}
            planned_addr = plan[1]['address'] if len(plan) > 1 else {}
            checks = [
                ('legacy_woo_customer_id_metafield', str(item['woo_customer_id']),
                 (live.get('metafield') or {}).get('value')),
                ('email', expected_payload.get('email'), live.get('email')),
                ('first_name', expected_payload.get('firstName'), live.get('firstName')),
                ('last_name', expected_payload.get('lastName'), live.get('lastName')),
                ('phone', expected_payload.get('phone'), live.get('phone')),
                ('address_count', str(len(plan) - 1), str(len(item['addresses']))),
                ('address1', planned_addr.get('address1'), addr.get('address1')),
                ('city', planned_addr.get('city'), addr.get('city')),
                ('zip', planned_addr.get('zip'), addr.get('zip')),
                ('countryCode', planned_addr.get('countryCode'), addr.get('countryCodeV2')),
                ('provinceCode', planned_addr.get('provinceCode'), addr.get('provinceCode')),
                ('company', planned_addr.get('company'), addr.get('company')),
                ('emailMarketingConsent', 'OMITTED',
                 'OMITTED' if 'emailMarketingConsent' not in expected_payload else 'SET'),
                # Only meaningful when a phone was dropped, and then it is the
                # check that matters: the tag is the only trace of the loss
                # visible inside Shopify itself.
                ('phone_dropped_tag',
                 rt.TAG_PHONE_DROPPED if item['phone_dropped'] else 'NOT_APPLICABLE',
                 rt.TAG_PHONE_DROPPED if rt.TAG_PHONE_DROPPED in (live.get('tags') or [])
                 else ('NOT_APPLICABLE' if not item['phone_dropped'] else 'TAG_MISSING')),
            ]
            for field, exp, act in checks:
                exp_n = '' if exp in (None, '') else str(exp)
                act_n = '' if act in (None, '') else str(act)
                if field == 'phone' and exp_n and act_n:
                    # Shopify normalises to E.164: "07700900123" comes back as
                    # "+447700900123". Comparing raw strings reports a mismatch
                    # for a number that is byte-for-byte the same subscriber, so
                    # compare canonical forms and keep the raw values visible.
                    if rt.phone_canonical(exp_n) == rt.phone_canonical(act_n):
                        exp_n = act_n
                recon.append({
                    'woo_customer_id': item['woo_customer_id'],
                    'shopify_customer_gid': item['gid'], 'field': field,
                    'expected_value': exp_n, 'actual_value': act_n,
                    'match': 'YES' if exp_n == act_n else 'NO',
                    'notes': '' if exp_n == act_n else 'live value differs from planned',
                })

        with open(RECON_PATH, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['woo_customer_id', 'shopify_customer_gid',
                                              'field', 'expected_value', 'actual_value',
                                              'match', 'notes'])
            w.writeheader()
            w.writerows(recon)
        mismatches = [r for r in recon if r['match'] == 'NO']
        print(f'  {len(recon)} field check(s), {len(mismatches)} mismatch(es)')
        for m in mismatches[:12]:
            print(f"    woo {m['woo_customer_id']} {m['field']}: "
                  f"planned vs live differ")

        # -------------------------------------------------------- delete
        print(f'\nDeleting {len(created)} test customer(s) (Gate 6: create -> '
              f'reconcile -> delete)...')
        for item in created:
            verify = graphql_request(
                domain, token, api_version,
                '{ customer(id: "%s") { id metafield(namespace: "custom", '
                'key: "legacy_woo_customer_id") { value } } }' % item['gid'])
            live_legacy = (((verify.get('data') or {}).get('customer') or {})
                           .get('metafield') or {}).get('value')
            if live_legacy != str(item['woo_customer_id']):
                raise Halt(f"refusing to delete {item['gid']}: legacy id is "
                           f'{live_legacy!r}, expected {item["woo_customer_id"]}')
            throttle.pace()
            dresp = send(domain, token, api_version, CUSTOMER_DELETE,
                         {'id': item['gid']}, throttle)
            mutations['customerDelete'] += 1
            dres = dresp['data']['customerDelete']
            ok = bool(dres.get('deletedCustomerId'))
            log({'ts': now(), 'run_id': run_id,
                 'woo_customer_id': item['woo_customer_id'],
                 'operation': 'customerDelete',
                 'status': 'DELETED' if ok else 'DELETE_FAILED',
                 'shopify_customer_gid': item['gid']})
            print(f"  woo {item['woo_customer_id']}: "
                  f"{'deleted' if ok else 'DELETE FAILED'}")

    except Halt as halt:
        print(f'\n*** HALTED: {halt}')
        log({'ts': now(), 'run_id': run_id, 'operation': 'run', 'status': 'HALTED',
             'detail': str(halt)[:200]})
        print(f'{len(created)} customer(s) were created before the halt and may still '
              f'exist. Nothing was auto-deleted.')
        return 1

    after = graphql_request(domain, token, api_version, '{ customersCount { count } }')
    remaining = after['data']['customersCount']['count']

    result = {
        'run_id': run_id, 'authorization': AUTHORIZATION,
        'cohort_size': len(cohort),
        'created': len(created), 'skipped_already_present': len(skipped),
        'failed': len(failed),
        'phone_fallbacks': len(phone_fallbacks),
        'customers_saved_by_phone_fallback': len(
            [w for w in phone_fallbacks if w not in failed]),
        'mutations': mutations, 'total_mutations': sum(mutations.values()),
        'reconciliation_checks': len(recon),
        'reconciliation_mismatches': len(mismatches),
        'customers_before': before, 'customers_after': remaining,
        'store_returned_to_empty': remaining == 0,
        'consent_set_on_any_customer': False,
        'measured_cost_points': sum(mutations.values()) * 10,
    }
    json.dump(result, open(RESULT_PATH, 'w', encoding='utf-8'), indent=2, sort_keys=True)

    print(f'\n{json.dumps(result, indent=2)}')
    print(f'\nStore: {before} -> {remaining} customers')
    return 0 if remaining == 0 and not failed else 1


if __name__ == '__main__':
    sys.exit(main())
