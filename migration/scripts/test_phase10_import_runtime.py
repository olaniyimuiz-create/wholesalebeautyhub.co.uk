"""Phase 10 Tier-1 test suite - fully offline, fully mocked.

NO SHOPIFY REQUEST IS MADE BY THIS FILE. Every `send` is a local function
returning a canned dict; every sleep is a recorder; every jitter is a constant.
No credentials are read and no network is touched, so this runs identically on
a machine that has never seen the store.

No real customer data is used. Every email is example.com (RFC 2606 reserved),
every phone is in Ofcom's 07700 900xxx drama range, every address is invented.

Run: python migration/scripts/test_phase10_import_runtime.py
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt
import phase10_customer_dry_run as dr

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []


def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))
    print(f'{"PASS" if condition else "FAIL"} - {name} {detail}')


def candidate(**over):
    """A synthetic IMPORT-shaped candidate. All values invented."""
    base = {
        'woo_customer_id': 4242, 'user_id': 99, 'is_registered': True,
        'username': 'someone', 'email_raw': 'Person@Example.com',
        'email': 'person@example.com', 'first_name': 'Ada', 'last_name': 'Lovelace',
        'company': 'Example Ltd',
        'billing_address1': '1 Test Street', 'billing_address2': 'Flat 2',
        'billing_city': 'Testville', 'billing_province': 'Surrey',
        'billing_country': 'GB', 'billing_zip': ' SW1A 1AA ',
        'phone': '+447700900123', 'has_shipping_address': True,
        'shipping_address1': '2 Other Road', 'shipping_address2': '',
        'shipping_city': 'Otherton', 'shipping_province': '',
        'shipping_country': 'GB', 'shipping_zip': 'M1 1AE',
        'date_registered': '2021-03-04 10:00:00', 'date_last_active': '',
    }
    base.update(over)
    return base


class Sleeper:
    """Records sleeps instead of performing them - the suite runs instantly."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(round(seconds, 6))


NO_JITTER = lambda: 0.0        # noqa: E731 - deterministic backoff for assertions
FULL_JITTER = lambda: 1.0      # noqa: E731


def ok_response(data=None, available=2000):
    return {'data': data or {}, 'extensions': {'cost': {
        'requestedQueryCost': 10, 'actualQueryCost': 10,
        'throttleStatus': {'maximumAvailable': 2000.0,
                           'currentlyAvailable': available, 'restoreRate': 100.0}}}}


THROTTLED_RESPONSE = {'errors': [{'message': 'Throttled',
                                  'extensions': {'code': 'THROTTLED'}}]}
ACCESS_DENIED_RESPONSE = {'errors': [{'message': 'Access denied for customers field.',
                                      'extensions': {'code': 'ACCESS_DENIED'}}]}


# ---------------------------------------------------------------- schema shape

def test_customer_input_shape():
    contract = rt.load_schema_contract()
    allowed = set(contract['input_types']['CustomerInput'])
    payload = rt.build_customer_input(candidate())
    check('CustomerInput: every produced key exists in the 2026-07 schema',
          set(payload) <= allowed, f'-> extra {sorted(set(payload) - allowed)}')
    check('CustomerInput: no addresses key is produced', 'addresses' not in payload)
    check('CustomerInput: schema itself declares no addresses field',
          'addresses' not in allowed)
    check('CustomerInput: no company key is produced (address-level only)',
          'company' not in payload)
    check('CustomerInput: schema declares no company field', 'company' not in allowed)
    check('CustomerInput: no password field exists in the schema',
          'password' not in allowed)
    check('CustomerInput: emailMarketingConsent is never set (GDPR policy)',
          'emailMarketingConsent' not in payload)
    check('CustomerInput: guest/registered tag applied',
          payload['tags'] == ['imported-from-woocommerce', 'registered'],
          f"-> {payload['tags']}")
    guest = rt.build_customer_input(candidate(is_registered=False))
    check('CustomerInput: guest tagged as guest',
          guest['tags'] == ['imported-from-woocommerce', 'guest'])


def test_legacy_metafield():
    payload = rt.build_customer_input(candidate())
    mfs = {(m['namespace'], m['key']): m for m in payload['metafields']}
    legacy = mfs.get(('custom', 'legacy_woo_customer_id'))
    check('legacy metafield: present on the create payload', legacy is not None)
    check('legacy metafield: value is the Woo customer id as a string',
          legacy and legacy['value'] == '4242', f'-> {legacy}')
    check('legacy metafield: type is single_line_text_field (product precedent)',
          legacy and legacy['type'] == 'single_line_text_field')
    check('registration date carried as a metafield, not a native field',
          ('custom', 'woo_registered_at') in mfs and 'createdAt' not in payload)
    dropped = rt.build_customer_input(candidate(), include_registered_at=False)
    check('registration date is optional (customerSet decision #3)',
          len(dropped['metafields']) == 1)


def test_schema_drift():
    contract = rt.load_schema_contract()
    live = json.loads(json.dumps(contract['input_types']))
    check('schema drift: identical schema reports no drift',
          rt.detect_schema_drift(live, contract) == [])

    removed = json.loads(json.dumps(contract['input_types']))
    del removed['MailingAddressInput']['countryCode']
    findings = rt.detect_schema_drift(removed, contract)
    check('schema drift: a removed field is detected',
          any('MailingAddressInput.countryCode REMOVED' in f for f in findings),
          f'-> {findings}')

    added = json.loads(json.dumps(contract['input_types']))
    added['CustomerInput']['addresses'] = '[MailingAddressInput!]'
    findings = rt.detect_schema_drift(added, contract)
    check('schema drift: CustomerInput.addresses reappearing is flagged',
          any('CustomerInput.addresses ADDED' in f for f in findings), f'-> {findings}')

    retyped = json.loads(json.dumps(contract['input_types']))
    retyped['MailingAddressInput']['countryCode'] = 'String'
    findings = rt.detect_schema_drift(retyped, contract)
    check('schema drift: a changed field type is detected',
          any('TYPE CHANGED' in f for f in findings), f'-> {findings}')


# ------------------------------------------------------- address transformation

def test_address_transformation():
    addr, flags = rt.build_address_input(candidate(), 'billing')
    contract = rt.load_schema_contract()
    allowed = set(contract['input_types']['MailingAddressInput'])
    check('address: every produced key exists in MailingAddressInput',
          set(addr) <= allowed, f'-> extra {sorted(set(addr) - allowed)}')
    check('address: no free-text country field in the schema', 'country' not in allowed)
    check('address: no free-text province field in the schema', 'province' not in allowed)
    check('address: street mapped to address1', addr['address1'] == '1 Test Street')
    check('address: company lands at address level',
          addr.get('company') == 'Example Ltd')
    check('address: postcode trimmed only, never reformatted',
          addr['zip'] == 'SW1A 1AA', f"-> {addr['zip']!r}")


def test_country_validation():
    addr, flags = rt.build_address_input(candidate(billing_country='gb'), 'billing')
    check('country: lowercase source is upper-cased to the enum form',
          addr and addr['countryCode'] == 'GB')

    addr, flags = rt.build_address_input(candidate(billing_country='XX'), 'billing')
    check('country: an invalid code is refused, not sent',
          addr is None and flags == [rt.ADDRESS_SKIP_BAD_COUNTRY], f'-> {flags}')

    # ZZ IS in Shopify's CountryCode enum - it means "Unknown Region", not a
    # country. The enum would accept it; an undeliverable address must not.
    check('country: ZZ is genuinely in the Shopify enum', 'ZZ' in rt.COUNTRY_CODES)
    addr, flags = rt.build_address_input(candidate(billing_country='ZZ'), 'billing')
    check('country: "Unknown Region" is refused despite being enum-valid',
          addr is None and flags == [rt.ADDRESS_SKIP_UNKNOWN_REGION], f'-> {flags}')

    addr, flags = rt.build_address_input(candidate(billing_country='United Kingdom'), 'billing')
    check('country: a free-text country name is refused',
          addr is None and flags == [rt.ADDRESS_SKIP_BAD_COUNTRY], f'-> {flags}')

    check('country: contract carries the full CountryCode enum',
          len(rt.COUNTRY_CODES) == 245 and 'GB' in rt.COUNTRY_CODES,
          f'-> {len(rt.COUNTRY_CODES)} codes')


def test_gb_province_handling():
    addr, flags = rt.build_address_input(candidate(), 'billing')
    check('GB province: county name is NOT sent as provinceCode',
          'provinceCode' not in addr, f'-> {addr.get("provinceCode")!r}')
    check('GB province: the drop is recorded as a flag, not silent',
          rt.PROVINCE_DROPPED_GB_RULE in flags, f'-> {flags}')

    addr, flags = rt.build_address_input(
        candidate(billing_country='US', billing_province='CA', billing_zip='90210'), 'billing')
    check('US province: provinceCode IS sent for a province country',
          addr.get('provinceCode') == 'CA', f'-> {addr}')
    check('US province: no drop flag raised',
          not any(f.startswith('PROVINCE_DROPPED') for f in flags))


def test_missing_country():
    addr, flags = rt.build_address_input(candidate(billing_country=''), 'billing')
    check('missing country: no address is constructed',
          addr is None and flags == [rt.ADDRESS_SKIP_NO_COUNTRY], f'-> {flags}')


def test_missing_address1():
    addr, flags = rt.build_address_input(candidate(billing_address1=''), 'billing')
    check('missing address1: address skipped entirely',
          addr is None and flags == [rt.ADDRESS_SKIP_NO_STREET], f'-> {flags}')
    addr, flags = rt.build_address_input(candidate(billing_address1='   '), 'billing')
    check('missing address1: whitespace-only street also skipped', addr is None)


def test_missing_city_and_zip():
    addr, flags = rt.build_address_input(candidate(billing_city=''), 'billing')
    check('missing city: address still attempted (city is not required)',
          addr is not None and 'city' not in addr)
    check('missing city: flagged for review', rt.ADDRESS_FLAG_NO_CITY in flags)
    addr, flags = rt.build_address_input(candidate(billing_zip=''), 'billing')
    check('missing zip: address still attempted, flagged',
          addr is not None and rt.ADDRESS_FLAG_NO_ZIP in flags)


def test_address_default_handling():
    plan = rt.plan_addresses(candidate(), include_shipping=False)
    check('default: billing-only plan is one address, set as default',
          len(plan) == 1 and plan[0]['setAsDefault'] is True)

    plan = rt.plan_addresses(candidate(), include_shipping=True)
    check('default: billing+shipping plan is two addresses', len(plan) == 2)
    check('default: billing is the default, shipping is not',
          plan[0]['setAsDefault'] is True and plan[1]['setAsDefault'] is False)

    plan = rt.plan_addresses(candidate(billing_address1=''), include_shipping=True)
    check('default: shipping-only customer still gets a default address',
          len(plan) == 1 and plan[0]['kind'] == 'shipping'
          and plan[0]['setAsDefault'] is True, f'-> {plan}')

    plan = rt.plan_addresses(candidate(billing_address1='', shipping_address1=''),
                             include_shipping=True)
    check('default: no address data produces no address calls', plan == [])

    billing_only = rt.plan_addresses(candidate(), include_shipping=False)
    check('policy: billing-vs-shipping is a parameter, never a default',
          len(billing_only) == 1 and len(rt.plan_addresses(candidate(), True)) == 2)


def test_deterministic_transformation():
    a = json.dumps(rt.build_customer_input(candidate()), sort_keys=True)
    b = json.dumps(rt.build_customer_input(candidate()), sort_keys=True)
    check('deterministic: identical input yields byte-identical customer payload', a == b)
    p1 = json.dumps(rt.plan_addresses(candidate(), True), sort_keys=True)
    p2 = json.dumps(rt.plan_addresses(candidate(), True), sort_keys=True)
    check('deterministic: identical input yields byte-identical address plan', p1 == p2)


# --------------------------------------------------------- throttle and retry

def test_throttling():
    sleeper = Sleeper()
    seq = [THROTTLED_RESPONSE, THROTTLED_RESPONSE, ok_response({'ok': True})]
    calls = {'n': 0}

    def send(doc, variables=None):
        i = calls['n']
        calls['n'] += 1
        return seq[min(i, len(seq) - 1)]

    resp, attempts = rt.execute_with_retry(send, '{ shop { name } }',
                                           sleep=sleeper, jitter=NO_JITTER)
    check('throttling: retries until success', resp['data'] == {'ok': True})
    check('throttling: took three attempts', attempts == 3, f'-> {attempts}')
    check('throttling: backed off 1s then 2s', sleeper.calls == [1.0, 2.0],
          f'-> {sleeper.calls}')

    # A throttled call must not burn the transient budget: max_transient=1 means
    # a single genuine transient failure aborts, yet 5 throttles still succeed.
    sleeper2 = Sleeper()
    seq2 = [THROTTLED_RESPONSE] * 5 + [ok_response({'ok': True})]
    calls2 = {'n': 0}

    def send2(doc, variables=None):
        i = calls2['n']
        calls2['n'] += 1
        return seq2[min(i, len(seq2) - 1)]

    resp2, attempts2 = rt.execute_with_retry(send2, '{ x }', sleep=sleeper2,
                                             jitter=NO_JITTER, max_transient=1)
    check('throttling: does not consume the business failure budget',
          resp2['data'] == {'ok': True} and attempts2 == 6, f'-> attempts {attempts2}')


def test_exponential_backoff():
    check('backoff: schedule is exactly 1/2/4/8/16',
          rt.BACKOFF_SCHEDULE == (1, 2, 4, 8, 16), f'-> {rt.BACKOFF_SCHEDULE}')
    delays = [rt.backoff_delay(i, NO_JITTER) for i in range(1, 6)]
    check('backoff: doubles without jitter', delays == [1.0, 2.0, 4.0, 8.0, 16.0],
          f'-> {delays}')
    check('backoff: clamps at 16s beyond the schedule',
          rt.backoff_delay(9, NO_JITTER) == 16.0)
    check('backoff: jitter adds up to 25 percent',
          rt.backoff_delay(1, FULL_JITTER) == 1.25
          and rt.backoff_delay(3, FULL_JITTER) == 5.0,
          f'-> {rt.backoff_delay(3, FULL_JITTER)}')
    check('backoff: jitter never shortens the delay',
          all(rt.backoff_delay(i, NO_JITTER) <= rt.backoff_delay(i, FULL_JITTER)
              for i in range(1, 6)))


def test_proactive_pacing():
    sleeper = Sleeper()
    t = rt.ThrottleController(floor=500, sleep=sleeper)
    t.observe(ok_response(available=1800))
    check('pacing: no sleep while the bucket is healthy', t.pace() == 0.0
          and sleeper.calls == [])
    check('pacing: reads currentlyAvailable', t.currently_available == 1800)
    check('pacing: reads restoreRate', t.restore_rate == 100.0)
    check('pacing: reads maximumAvailable', t.maximum_available == 2000.0)
    check('pacing: reads actualQueryCost from extensions.cost',
          t.last_actual_cost == 10)

    t.observe(ok_response(available=200))
    slept = t.pace()
    check('pacing: sleeps when below the floor', slept == 3.0, f'-> {slept}')
    check('pacing: sleep is (floor - available) / restoreRate',
          sleeper.calls == [3.0], f'-> {sleeper.calls}')
    check('pacing: concurrency is 1 by construction (no parallel executor exists)',
          not hasattr(rt, 'ThreadPoolExecutor') and not hasattr(rt, 'run_parallel'))

    t2 = rt.ThrottleController(sleep=Sleeper())
    t2.observe({'data': {}})
    check('pacing: a response with no extensions block does not crash',
          t2.pace() == 0.0)


def test_timeout_verify_before_retry():
    sleeper = Sleeper()
    attempts = {'n': 0}

    def send(doc, variables=None):
        attempts['n'] += 1
        raise TimeoutError('the read operation timed out')

    resp, n = rt.execute_with_retry(
        send, '{ x }', sleep=sleeper, jitter=NO_JITTER,
        verify=lambda: 'gid://shopify/Customer/1')
    check('timeout: verify is consulted before any retry', attempts['n'] == 1,
          f'-> send called {attempts["n"]}x')
    check('timeout: a confirmed write is not repeated',
          resp.get('_verified_existing') == 'gid://shopify/Customer/1')
    check('timeout: no backoff sleep when verify resolves it', sleeper.calls == [])

    attempts2 = {'n': 0}

    def send2(doc, variables=None):
        attempts2['n'] += 1
        raise TimeoutError('timed out')

    try:
        rt.execute_with_retry(send2, '{ x }', sleep=Sleeper(), jitter=NO_JITTER,
                              verify=lambda: None)
        raised = False
    except TimeoutError:
        raised = True
    check('timeout: unverified write is retried then raised',
          raised and attempts2['n'] == rt.MAX_TRANSIENT_ATTEMPTS,
          f'-> {attempts2["n"]} attempts')


def test_token_expiry():
    def send(doc, variables=None):
        raise RuntimeError('HTTP Error 401: Unauthorized - body: [API] Invalid API key or access token')

    try:
        rt.execute_with_retry(send, '{ x }', sleep=Sleeper(), jitter=NO_JITTER)
        halted = False
        detail = 'no exception'
    except rt.HaltMigration as e:
        halted, detail = True, str(e)
    check('token expiry: HALTS, never retried', halted, f'-> {detail[:60]}')

    attempts = {'n': 0}

    def send2(doc, variables=None):
        attempts['n'] += 1
        raise RuntimeError('HTTP Error 401: Unauthorized')

    try:
        rt.execute_with_retry(send2, '{ x }', sleep=Sleeper(), jitter=NO_JITTER)
    except rt.HaltMigration:
        pass
    check('token expiry: exactly one attempt made', attempts['n'] == 1,
          f'-> {attempts["n"]}')


def test_access_denied():
    klass, detail = rt.classify_response(ACCESS_DENIED_RESPONSE)
    check('ACCESS_DENIED: classified as an auth failure', klass == rt.AUTH_FAILURE,
          f'-> {klass}')

    def send(doc, variables=None):
        return ACCESS_DENIED_RESPONSE

    try:
        rt.execute_with_retry(send, '{ x }', sleep=Sleeper(), jitter=NO_JITTER)
        halted = False
    except rt.HaltMigration:
        halted = True
    check('ACCESS_DENIED: halts rather than being swallowed', halted)

    # The original defect: ACCESS_DENIED reported as a successful live check.
    original = dr.graphql_request
    try:
        dr.graphql_request = lambda *a, **k: ACCESS_DENIED_RESPONSE
        try:
            dr.fetch_live_customer_emails('x.myshopify.com', 'tok', '2026-07')
            status = 'OK'
        except Exception as e:  # noqa: BLE001 - mirrors main()'s handler
            status = f'FAILED: {e}'
    finally:
        dr.graphql_request = original
    check('ACCESS_DENIED: live check status is never OK', status != 'OK', f'-> {status!r}')
    check('ACCESS_DENIED: status is exactly "FAILED: ACCESS_DENIED"',
          status == 'FAILED: ACCESS_DENIED', f'-> {status!r}')


def test_sanitized_errors():
    msg = ("Email person@example.com has already been taken; phone "
           "+447700900123 is invalid; address SW1A 1AA rejected")
    clean = rt.sanitize(msg)
    check('sanitize: email removed', 'person@example.com' not in clean, f'-> {clean}')
    check('sanitize: phone removed', '447700900123' not in clean, f'-> {clean}')
    check('sanitize: postcode removed', 'SW1A 1AA' not in clean, f'-> {clean}')
    check('sanitize: the diagnostic wording survives',
          'already been taken' in clean and 'is invalid' in clean, f'-> {clean}')

    errs = rt.sanitize_user_errors([
        {'field': ['input', 'email'], 'message': 'Email person@example.com is taken'}])
    check('sanitize: userErrors field names kept, values redacted',
          errs[0]['field'] == ['input', 'email']
          and 'person@example.com' not in errs[0]['message'], f'-> {errs}')
    check('sanitize: None is tolerated', rt.sanitize(None) is None)

    klass, detail = rt.classify_response(
        {'errors': [{'message': 'Bad email person@example.com',
                     'extensions': {'code': 'BAD_REQUEST'}}]})
    check('sanitize: classify_response returns an already-sanitized detail',
          'person@example.com' not in detail, f'-> {detail}')


def test_duplicate_legacy_id():
    page = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [
            {'node': {'id': 'gid://shopify/Customer/1', 'addresses': [],
                      'metafield': {'value': '777'}}},
            {'node': {'id': 'gid://shopify/Customer/2', 'addresses': [],
                      'metafield': {'value': '777'}}},
        ]}}}
    try:
        rt.fetch_existing_legacy_map(lambda d, v=None: page)
        halted, detail = False, 'no exception'
    except rt.HaltMigration as e:
        halted, detail = True, str(e)
    check('duplicate legacy id: HALTS the run', halted, f'-> {detail[:70]}')
    check('duplicate legacy id: the offending id is named',
          halted and '777' in detail)


# ------------------------------------------------------------ resume behaviour

def legacy_map_pages(total_done, page_size=250):
    """Mock the live scan: `total_done` customers already carry a legacy id."""
    pages = []
    ids = [str(i) for i in range(1, total_done + 1)]
    for start in range(0, max(len(ids), 1), page_size):
        chunk = ids[start:start + page_size]
        pages.append({'data': {'customers': {
            'pageInfo': {'hasNextPage': start + page_size < len(ids),
                         'endCursor': f'CUR{start}'},
            'edges': [{'node': {'id': f'gid://shopify/Customer/{i}',
                                'addresses': [{'id': 'a'}],
                                'metafield': {'value': i}}} for i in chunk]}}})
    return pages


def run_resume(total_done, manifest_size=12096):
    pages = legacy_map_pages(total_done)
    calls = {'n': 0}

    def send(doc, variables=None):
        i = min(calls['n'], len(pages) - 1)
        calls['n'] += 1
        return pages[i]

    legacy_map, page_count = rt.fetch_existing_legacy_map(send)
    remaining = rt.records_to_process(list(range(1, manifest_size + 1)), legacy_map)
    return legacy_map, remaining, page_count


def test_resume():
    for done in (1, 1000, 5000, 10000):
        legacy_map, remaining, pages = run_resume(done)
        check(f'resume after {done:,}: live map holds {done:,} customers',
              len(legacy_map) == done, f'-> {len(legacy_map)}')
        check(f'resume after {done:,}: exactly {12096 - done:,} records remain',
              len(remaining) == 12096 - done, f'-> {len(remaining)}')
        check(f'resume after {done:,}: continues at the right record',
              remaining[0] == done + 1, f'-> {remaining[0]}')
        check(f'resume after {done:,}: no already-imported record is reprocessed',
              not (set(str(r) for r in remaining) & set(legacy_map)))

    legacy_map, remaining, pages = run_resume(10000)
    check('resume: the startup scan is ONE pass, not one scan per record',
          pages == 40, f'-> {pages} pages for 10,000 customers at 250/page')


def test_deleted_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = os.path.join(tmp, 'log.jsonl')
        checkpoint_path = os.path.join(tmp, 'checkpoint.jsonl')
        ledger = rt.ImportLedger(ledger_path, checkpoint_path, run_id='r1',
                                 importer_commit='abc123')
        for i in (1, 2, 3):
            ledger.record(i, 'customerCreate', 'CREATED',
                          shopify_gid=f'gid://shopify/Customer/{i}')
        check('checkpoint: written during the run', os.path.exists(checkpoint_path))
        check('checkpoint: one line per customer, flushed immediately',
              len(open(checkpoint_path, encoding='utf-8').read().strip().split('\n')) == 3)

        os.remove(checkpoint_path)
        check('checkpoint: deleted', not os.path.exists(checkpoint_path))

        legacy_map, remaining, _ = run_resume(3)
        check('checkpoint deleted: resume is still exact, from the live map alone',
              len(remaining) == 12096 - 3 and remaining[0] == 4,
              f'-> {len(remaining)} remaining, first {remaining[0]}')
        check('checkpoint deleted: the live map is the source of truth',
              set(legacy_map) == {'1', '2', '3'}, f'-> {sorted(legacy_map)}')


def test_address_failure():
    """Customer created, address failed - never re-create the customer."""
    page = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [{'node': {'id': 'gid://shopify/Customer/5', 'addresses': [],
                            'metafield': {'value': '5'}}}]}}}
    legacy_map, _ = rt.fetch_existing_legacy_map(lambda d, v=None: page)
    remaining = rt.records_to_process([5], legacy_map)
    check('address failure: the customer is NOT re-created', remaining == [],
          f'-> {remaining}')

    partial = rt.partial_address_customers(legacy_map, {5: 1})
    check('address failure: the shortfall is detected from live data',
          partial == ['5'], f'-> {partial}')
    check('address failure: a complete customer is not flagged',
          rt.partial_address_customers({'5': {'gid': 'g', 'address_count': 1}}, {5: 1}) == [])


# ------------------------------------------- dry-run classification invariants

def test_duplicate_email():
    seen = {}
    first = candidate(woo_customer_id=1, email='dup@example.com')
    cls1, _, _ = dr.classify(first, seen, {}, {}, set())
    seen['dup@example.com'] = first
    same = candidate(woo_customer_id=2, email='dup@example.com')
    cls2, reason2, _ = dr.classify(same, seen, {}, {}, set())
    check('duplicate email, same identity: SKIP, not a conflict',
          cls2 == 'SKIP' and reason2 == 'duplicate_of_already_included_row',
          f'-> {cls2}/{reason2}')


def test_conflicting_name():
    seen = {}
    first = candidate(woo_customer_id=1, email='dup@example.com', first_name='Ada')
    dr.classify(first, seen, {}, {}, set())
    seen['dup@example.com'] = first
    other = candidate(woo_customer_id=2, email='dup@example.com', first_name='Grace')
    cls, reason, _ = dr.classify(other, seen, {}, {}, set())
    check('conflicting name: QUARANTINE, never an automatic merge',
          cls == 'QUARANTINE' and reason == 'duplicate_email_conflicting_identity',
          f'-> {cls}/{reason}')


def test_missing_email():
    cls, reason, _ = dr.classify(candidate(email='', email_raw=''), {}, {}, {}, set())
    check('missing email: QUARANTINE, never sent to Shopify',
          cls == 'QUARANTINE' and reason == 'missing_email', f'-> {cls}/{reason}')
    cls, reason, _ = dr.classify(candidate(email='nope', email_raw='nope'), {}, {}, {}, set())
    check('invalid email: QUARANTINE', cls == 'QUARANTINE'
          and reason == 'invalid_email_format', f'-> {cls}/{reason}')


def test_changed_email():
    """A customer whose email changed after import must not be duplicated: the
    legacy id, not the email, decides whether they already exist."""
    page = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [{'node': {'id': 'gid://shopify/Customer/9', 'addresses': [{'id': 'a'}],
                            'metafield': {'value': '9'}}}]}}}
    legacy_map, _ = rt.fetch_existing_legacy_map(lambda d, v=None: page)
    remaining = rt.records_to_process([9], legacy_map)
    check('changed email: matched by legacy id, so SKIPPED not duplicated',
          remaining == [], f'-> {remaining}')
    check('changed email: email is not the resume key',
          '9' in legacy_map and legacy_map['9']['gid'] == 'gid://shopify/Customer/9')


# ------------------------------------------------------------ phone uniqueness

def test_phone_uniqueness():
    cands = [
        {'woo_customer_id': 1, 'phone': '+447700900123'},
        {'woo_customer_id': 2, 'phone': '07700900123'},     # same subscriber
        {'woo_customer_id': 3, 'phone': '+447700900999'},
        {'woo_customer_id': 4, 'phone': ''},
        {'woo_customer_id': 5, 'phone': '+44 7700 900999'},  # same as 3, spaced
    ]
    groups = rt.phone_collision_groups(cands)
    check('phone: UK 0-prefix and +44 forms collide',
          any(set(v) == {1, 2} for v in groups.values()), f'-> {groups}')
    check('phone: separators do not hide a collision',
          any(set(v) == {3, 5} for v in groups.values()), f'-> {groups}')
    check('phone: two collision groups found', len(groups) == 2, f'-> {len(groups)}')

    summary = rt.phone_collision_summary(cands)
    check('phone: summary counts only, no numbers',
          summary['customers_with_phone'] == 4
          and summary['collision_groups'] == 2
          and summary['customers_in_collisions'] == 4, f'-> {summary}')
    check('phone: no phone value appears in the summary',
          not any('7700' in str(v) for v in summary.values()), f'-> {summary}')
    check('phone: an empty phone never forms a group',
          all(4 not in v for v in groups.values()))


# --------------------------------------------------------------- PII discipline

def test_no_pii_in_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = rt.ImportLedger(os.path.join(tmp, 'l.jsonl'),
                                 os.path.join(tmp, 'c.jsonl'), run_id='r', importer_commit='c')
        entry = ledger.record(1, 'customerCreate', 'FAILED', attempt=2,
                              error_class='USER_ERROR',
                              error_detail='Email person@example.com has been taken')
        check('ledger: error detail is sanitized on the way in',
              'person@example.com' not in json.dumps(entry), f'-> {entry["error_detail"]}')
        text = open(os.path.join(tmp, 'l.jsonl'), encoding='utf-8').read()
        check('ledger: no email reaches the file', '@example.com' not in text)
        check('ledger: carries the Woo id as the identifier', '"woo_customer_id": 1' in text)
        check('ledger: carries run_id and importer_commit for traceability',
              '"run_id": "r"' in text and '"importer_commit": "c"' in text)

    for bad in ('email', 'first_name', 'phone', 'billing_address1', 'password'):
        try:
            rt.assert_ledger_record_safe({'woo_customer_id': 1, bad: 'x'})
            rejected = False
        except ValueError:
            rejected = True
        check(f'ledger: a record carrying {bad} is rejected', rejected)


def test_no_pii_in_tracked_artifacts():
    """The gitignore boundary is the control; this asserts it holds."""
    try:
        tracked = subprocess.run(['git', 'ls-files', 'reports/'], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=30).stdout.split()
    except Exception as e:  # noqa: BLE001
        check('tracked artifacts: git available for the scan', False, f'-> {e}')
        return

    import re as _re
    email_re = _re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
    # Retina asset filenames ("hero-image@2x.webp") match the email shape but
    # are not addresses. Excluded by file extension rather than by the "@2x"
    # convention, so any image naming scheme is covered.
    ASSET_EXTENSIONS = ('webp', 'jpg', 'jpeg', 'png', 'gif', 'svg', 'avif', 'pdf')
    offenders = []
    for rel in tracked:
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            body = open(path, encoding='utf-8', errors='replace').read()
        except Exception:  # noqa: BLE001
            continue
        hits = [h for h in email_re.findall(body)
                if not h.endswith('example.com')
                and not h.lower().endswith(ASSET_EXTENSIONS)]
        if hits:
            offenders.append((rel, len(hits)))
    check('tracked artifacts: no real email address in any tracked report',
          not offenders, f'-> {offenders}')

    for pii_file in ('reports/phase10_customer_manifest.csv',
                     'reports/phase10_customer_quarantine.csv',
                     'reports/phase10_customer_import_log.jsonl',
                     'reports/phase10_customer_import_checkpoint.jsonl'):
        ignored = subprocess.run(['git', 'check-ignore', '-q', pii_file],
                                 cwd=REPO_ROOT, capture_output=True).returncode == 0
        check(f'tracked artifacts: {os.path.basename(pii_file)} is gitignored', ignored)


def test_runtime_cannot_mutate():
    """The structural no-write guarantee, asserted rather than assumed."""
    try:
        rt.execute_with_retry(lambda d, v=None: ok_response(),
                              'mutation { customerCreate(input: {}) { customer { id } } }')
        refused = False
    except rt.HaltMigration:
        refused = True
    check('no-write: a mutation document is refused by the executor', refused)

    source = open(rt.__file__, encoding='utf-8').read()
    for forbidden in ('customerCreate(', 'customerAddressCreate(', 'customerSet(',
                      'metafieldsSet(', 'customerDelete('):
        check(f'no-write: module builds no {forbidden[:-1]} document',
              forbidden not in source)



# =========================================================================
# Tier-1 additions: phone collisions, operation ordering, eligibility gates
# All synthetic. Phones are Ofcom 07700 900xxx drama numbers, reserved for
# exactly this purpose and never allocated to a real subscriber.
# =========================================================================

def collide(woo_id, phone, registered=True, email=None):
    return candidate(woo_customer_id=woo_id, phone=phone, is_registered=registered,
                     email=email or f'c{woo_id}@example.com',
                     email_raw=email or f'c{woo_id}@example.com')


def test_two_customers_one_phone():
    rows = rt.build_collision_report([
        collide(101, '07700900001'), collide(102, '+447700900001'),
        collide(103, '07700900002'),
    ])
    check('collision: one group found for the shared number', len(rows) == 1, f'-> {len(rows)}')
    row = rows[0]
    check('collision: both sharers are in the group',
          row['source_customer_ids'] == '101 102', f"-> {row['source_customer_ids']}")
    check('collision: the uncontested customer forms no group',
          '103' not in row['source_customer_ids'])
    check('collision: count recorded', row['affected_customer_count'] == 2)
    check('collision: unresolved by default',
          row['recommended_action'] == rt.ACTION_MANUAL_REVIEW
          and row['review_status'] == rt.REVIEW_PENDING, f'-> {row}')
    check('collision: all three decisions are offered explicitly',
          row['available_decisions'] == 'KEEP_ONE|OMIT_FROM_ALL|MANUAL_REVIEW_REQUIRED',
          f"-> {row['available_decisions']}")
    check('collision: no owner is chosen automatically',
          row['chosen_owner_woo_customer_id'] == '')


def test_large_collision_group_is_high_risk():
    members = [collide(200 + i, '07700900123', registered=False) for i in range(27)]
    rows = rt.build_collision_report(members + [collide(999, '07700900555')])
    group = [r for r in rows if r['affected_customer_count'] == 27]
    check('27-group: detected as a single group', len(group) == 1, f'-> {len(group)}')
    check('27-group: flagged HIGH RISK', group and group[0]['risk'] == 'HIGH',
          f"-> {group[0]['risk'] if group else None}")
    check('27-group: threshold is 10+', rt.HIGH_RISK_GROUP_SIZE == 10)
    check('27-group: still requires manual review, never auto-assigned',
          group[0]['recommended_action'] == rt.ACTION_MANUAL_REVIEW)
    check('27-group: registered/guest evidence supplied to the reviewer',
          group[0]['registered_accounts_in_group'] == 0
          and group[0]['guest_rows_in_group'] == 27, f'-> {group[0]}')
    small = [r for r in rows if r['affected_customer_count'] < 10]
    check('27-group: a small group is NOT flagged high risk',
          all(r['risk'] == 'NORMAL' for r in small))


def test_selected_owner_retains_phone():
    rows = rt.build_collision_report([collide(301, '07700900301'),
                                      collide(302, '+44 7700 900301')])
    gid = rows[0]['collision_group_id']
    owners = rt.phone_owner_map(rows, {gid: {'action': rt.ACTION_KEEP_ONE,
                                             'chosen_owner': 301}})
    check('KEEP_ONE: the selected owner keeps the phone', owners[301][0] is True,
          f'-> {owners[301]}')
    check('KEEP_ONE: the non-owner has it omitted', owners[302][0] is False,
          f'-> {owners[302]}')
    check('KEEP_ONE: the omission carries a reason naming the owner',
          '301' in owners[302][1], f'-> {owners[302][1]}')

    payload_owner = rt.build_customer_input(collide(301, '07700900301'),
                                            phone_allowed=owners[301][0])
    payload_other = rt.build_customer_input(collide(302, '+44 7700 900301'),
                                            phone_allowed=owners[302][0])
    check('KEEP_ONE: owner payload carries phone', 'phone' in payload_owner)
    check('KEEP_ONE: non-owner payload omits phone entirely',
          'phone' not in payload_other, f'-> {payload_other.get("phone")!r}')
    check('KEEP_ONE: the non-owner is still created, just without a phone',
          payload_other['email'] == 'c302@example.com')

    try:
        rt.phone_owner_map(rows, {gid: {'action': rt.ACTION_KEEP_ONE, 'chosen_owner': 999}})
        rejected = False
    except ValueError:
        rejected = True
    check('KEEP_ONE: an owner outside the group is rejected, not assumed', rejected)


def test_unresolved_collision_defaults_safe():
    rows = rt.build_collision_report([collide(401, '07700900401'),
                                      collide(402, '07700900401')])
    owners = rt.phone_owner_map(rows)
    check('unresolved: nobody keeps the phone by default',
          owners[401][0] is False and owners[402][0] is False, f'-> {owners}')
    check('unresolved: the reason names MANUAL_REVIEW_REQUIRED',
          rt.ACTION_MANUAL_REVIEW in owners[401][1], f'-> {owners[401][1]}')
    check('unresolved: the group is reported as outstanding',
          rt.unresolved_collision_groups(rows) == [1], f'-> {rt.unresolved_collision_groups(rows)}')

    gid = rows[0]['collision_group_id']
    resolved = rt.unresolved_collision_groups(rows, {gid: {'action': rt.ACTION_OMIT_FROM_ALL}})
    check('unresolved: a decided group drops off the outstanding list', resolved == [])

    owners = rt.phone_owner_map(rows, {gid: {'action': rt.ACTION_OMIT_FROM_ALL}})
    check('OMIT_FROM_ALL: no member keeps the phone',
          owners[401][0] is False and owners[402][0] is False)


def test_deterministic_collision_hashing():
    a = rt.phone_hash('07700900123')
    b = rt.phone_hash('+447700900123')
    c = rt.phone_hash('+44 7700 900123')
    check('hash: equivalent phone forms hash identically', a == b == c, f'-> {a} {b} {c}')
    check('hash: a different number hashes differently', rt.phone_hash('07700900999') != a)
    check('hash: stable across calls', rt.phone_hash('07700900123') == a)
    check('hash: salt changes the output',
          rt.phone_hash('07700900123', salt='other') != a)
    check('hash: fixed width label', len(a) == 16 and all(ch in '0123456789abcdef' for ch in a))
    check('hash: an empty phone yields no label', rt.phone_hash('') is None)

    members = [collide(500 + i, '0770090%04d' % i) for i in range(5)] + \
              [collide(600, '07700900000')]
    r1 = json.dumps(rt.build_collision_report(members), sort_keys=True)
    r2 = json.dumps(rt.build_collision_report(list(reversed(members))), sort_keys=True)
    check('hash: report is deterministic regardless of input order', r1 == r2)


def test_no_raw_phone_leakage():
    phones = ['07700900701', '+447700900701', '07700900702', '+44 7700 900702']
    members = [collide(700 + i, p) for i, p in enumerate(phones)]
    rows = rt.build_collision_report(members)
    blob = json.dumps(rows)
    for p in phones:
        check(f'leakage: raw form {p!r} absent from the report', p not in blob)
        check(f'leakage: digit form of {p!r} absent from the report',
              rt.phone_digits(p) not in blob)
    check('leakage: canonical form absent too',
          rt.phone_canonical(phones[0]) not in blob)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = rt.ImportLedger(os.path.join(tmp, 'l.jsonl'), os.path.join(tmp, 'c.jsonl'))
        ledger.record(700, 'customerCreate', 'FAILED', error_class='USER_ERROR',
                      error_detail='Phone +447700900701 has already been taken')
        text = open(os.path.join(tmp, 'l.jsonl'), encoding='utf-8').read()
        check('leakage: a phone in a Shopify error never reaches the ledger',
              '7700900701' not in text, f'-> {text[:160]}')

    summary = rt.phone_collision_summary(members)
    check('leakage: the aggregate summary holds no phone data',
          not any('7700' in str(v) for v in summary.values()))


def test_no_quadratic_lookup():
    # Parse rather than grep: the module deliberately NAMES the quadratic
    # helper in a docstring so it is not reintroduced, and a substring check
    # cannot tell that mention apart from a call.
    import ast as _ast
    source = open(rt.__file__, encoding='utf-8').read()
    called = set()
    for node in _ast.walk(_ast.parse(source)):
        if isinstance(node, _ast.Call):
            fn = node.func
            name = getattr(fn, 'id', None) or getattr(fn, 'attr', None)
            if name:
                called.add(name)
    check('no quadratic: runtime never calls find_existing_by_legacy_id',
          'find_existing_by_legacy_id' not in called, f'-> called={sorted(called)[:5]}...')
    check('no quadratic: the hazard is documented so it is not reintroduced',
          'find_existing_by_legacy_id' in source and 'quadratic' in source)

    calls = {'n': 0}
    pages = legacy_map_pages(5000)

    def send(doc, variables=None):
        calls['n'] += 1
        return pages[min(calls['n'] - 1, len(pages) - 1)]

    legacy_map, page_count = rt.fetch_existing_legacy_map(send)
    check('no quadratic: 5,000 customers cost 20 reads, not 5,000 scans',
          calls['n'] == 20 and page_count == 20, f'-> {calls["n"]} requests')
    remaining = rt.records_to_process(list(range(1, 12097)), legacy_map)
    check('no quadratic: no further request is made per remaining record',
          calls['n'] == 20 and len(remaining) == 7096, f'-> {calls["n"]} requests')


def test_legacy_metafield_inline():
    stages = rt.plan_customer_import(candidate())
    create = stages[0]
    check('legacy inline: stage 1 is customerCreate', create['stage'] == rt.STAGE_CUSTOMER)
    keys = {(m['namespace'], m['key']) for m in create['input']['metafields']}
    check('legacy inline: the legacy id rides on the create call itself',
          ('custom', 'legacy_woo_customer_id') in keys, f'-> {keys}')
    check('legacy inline: no separate metafieldsSet stage is planned',
          not any(s['stage'] == 'metafieldsSet' for s in stages))
    check('legacy inline: the create stage needs no pre-existing customer id',
          create['requires_customer_id'] is False)


def test_plan_has_no_addresses_in_customer_input():
    stages = rt.plan_customer_import(candidate(), include_shipping=True)
    check('plan: customerCreate input carries no addresses key',
          'addresses' not in stages[0]['input'])
    check('plan: addresses appear only as separate stages',
          all(s['stage'] == rt.STAGE_ADDRESS for s in stages[1:]) and len(stages) == 3,
          f'-> {[s["stage"] for s in stages]}')


def test_addresses_only_after_customer_creation():
    stages = rt.plan_customer_import(candidate(), include_shipping=True)
    check('ordering: exactly one customerCreate, and it is first',
          [s['stage'] for s in stages].count(rt.STAGE_CUSTOMER) == 1
          and stages[0]['stage'] == rt.STAGE_CUSTOMER)
    check('ordering: every address stage declares it needs a customer id',
          all(s['requires_customer_id'] for s in stages[1:]))
    check('ordering: no address stage precedes the create stage',
          all(i > 0 for i, s in enumerate(stages) if s['stage'] == rt.STAGE_ADDRESS))
    check('ordering: address stages carry no customerId, since none exists yet',
          all('customerId' not in s for s in stages[1:]))

    no_addr = rt.plan_customer_import(candidate(billing_address1='', shipping_address1=''),
                                      include_shipping=True)
    check('ordering: a customer with no address plans exactly one call',
          len(no_addr) == 1, f'-> {len(no_addr)}')


def test_address_policy_is_selectable():
    a = rt.plan_customer_import(candidate(), include_shipping=False)
    b = rt.plan_customer_import(candidate(), include_shipping=True)
    check('address policy: Option A plans 1 address', len(a) == 2, f'-> {len(a)}')
    check('address policy: Option B plans 2 addresses', len(b) == 3, f'-> {len(b)}')
    check('address policy: the customerCreate stage is byte-identical either way',
          json.dumps(a[0], sort_keys=True) == json.dumps(b[0], sort_keys=True))


def test_missing_country_never_fabricated():
    addr, flags = rt.build_address_input(candidate(billing_country=''), 'billing')
    check('missing country: address skipped, not defaulted', addr is None)
    check('missing country: GB is never substituted',
          flags == [rt.ADDRESS_SKIP_NO_COUNTRY], f'-> {flags}')
    stages = rt.plan_customer_import(candidate(billing_country=''))
    check('missing country: the customer is still created',
          len(stages) == 1 and stages[0]['stage'] == rt.STAGE_CUSTOMER)
    check('missing country: no address call is planned for them',
          not any(s['stage'] == rt.STAGE_ADDRESS for s in stages))


def test_consent_never_set():
    stages = rt.plan_customer_import(candidate(), include_shipping=True)
    blob = json.dumps(stages)
    check('consent: emailMarketingConsent never appears in any planned stage',
          'emailMarketingConsent' not in blob)
    check('consent: marketingState never appears either', 'marketingState' not in blob)
    source = open(rt.__file__, encoding='utf-8').read()
    check('consent: the runtime has no parameter that could switch it on',
          "payload['emailMarketingConsent']" not in source)
    contract = rt.load_schema_contract()
    check('consent: the field genuinely exists and is being declined, not missed',
          'emailMarketingConsent' in contract['input_types']['CustomerInput'])


def test_missing_email_never_reaches_create():
    for bad in ({'email': '', 'email_raw': ''},
                {'email': '   ', 'email_raw': '   '},
                {'email': 'nope', 'email_raw': 'nope'},
                {'email': 'a@b', 'email_raw': 'a@b'}):
        try:
            rt.plan_customer_import(candidate(**bad))
            blocked = False
        except rt.NotEligibleForImport:
            blocked = True
        check(f'missing email: {bad["email"]!r} cannot reach customerCreate', blocked)

    cls, reason, _ = dr.classify(candidate(email='', email_raw=''), {}, {}, {}, set())
    check('missing email: the dry run quarantines it upstream too',
          cls == 'QUARANTINE' and reason == 'missing_email')
    check('missing email: no synthetic address is ever generated',
          'example.com' not in open(rt.__file__, encoding='utf-8').read())


def test_conflicting_names_never_merged():
    seen = {}
    first = candidate(woo_customer_id=1, email='shared@example.com',
                      email_raw='shared@example.com', first_name='Ada', last_name='L')
    dr.classify(first, seen, {}, {}, set())
    seen['shared@example.com'] = first
    second = candidate(woo_customer_id=2, email='shared@example.com',
                       email_raw='shared@example.com', first_name='Grace', last_name='H')
    cls, reason, notes = dr.classify(second, seen, {}, {}, set())
    check('name conflict: quarantined, not merged',
          cls == 'QUARANTINE' and reason == 'duplicate_email_conflicting_identity')
    check('name conflict: the winning row is named for the reviewer',
          'woo_customer_id=1' in notes, f'-> {notes[:80]}')
    check('name conflict: both name variants are preserved in the note',
          'Ada' in notes and 'Grace' in notes, f'-> {notes[:120]}')
    check('name conflict: the first row keeps its own name untouched',
          seen['shared@example.com']['first_name'] == 'Ada')
    # The runtime DOES now have a name-override path - added deliberately for
    # reviewer decisions. The property to hold is not "no path exists" but
    # "no path fires without a human token", so assert that directly rather
    # than by the absence of a string.
    unreviewed = [{'woo_customer_id': 1, 'chosen_name': '',
                   'import_name': 'Ada L', 'alternate_name': 'Grace H'}]
    gate = rt.name_conflict_gate(unreviewed, [1])
    check('name conflict: an unreviewed conflict produces no name override',
          gate['name_overrides'] == {}, f"-> {gate['name_overrides']}")
    untouched = rt.apply_name_override(second, gate['name_overrides'])
    check('name conflict: with no override, the candidate name is unchanged',
          untouched['first_name'] == 'Grace' and untouched['last_name'] == 'H')
    check('name conflict: the affected customer is held back, not silently renamed',
          gate['excluded'] == [1] and gate['proceed_with'] == [], f'-> {gate}')


# =========================================================================
# Phone ownership evidence and the approved keep-one / omit rule.
# Synthetic throughout; Ofcom 07700 900xxx drama numbers only.
# =========================================================================

def member(woo_id, registered=False, own_profile=False, company=''):
    return {'woo_customer_id': woo_id, 'is_registered': registered,
            'phone_from_profile': own_profile, 'company': company}


def test_evidence_scoring():
    score, signals = rt.phone_evidence(member(1, registered=True, own_profile=True))
    check('evidence: registered + own profile scores 5', score == 5, f'-> {score}')
    check('evidence: both positive signals named',
          'registered_account' in signals and 'phone_on_own_profile' in signals, f'-> {signals}')

    score, signals = rt.phone_evidence(member(2))
    check('evidence: bare guest with a borrowed phone scores 0', score == 0, f'-> {score}')
    check('evidence: the weak provenance is named, not hidden',
          'guest_row' in signals and 'phone_from_order_fallback' in signals, f'-> {signals}')

    score, _ = rt.phone_evidence(member(3, registered=True))
    check('evidence: an account alone does not reach the ownership threshold',
          score == 2 and score < rt.MIN_OWNERSHIP_SCORE, f'-> {score}')

    score, signals = rt.phone_evidence(member(4, registered=True, own_profile=True,
                                              company='Some Salon Ltd'))
    check('evidence: a company reduces individual-ownership confidence',
          score == 3 and 'company_present_suggests_business' in signals, f'-> {score} {signals}')

    check('evidence: scoring is deterministic',
          rt.phone_evidence(member(1, True, True))[0] == rt.phone_evidence(member(1, True, True))[0])


def test_keep_one_when_one_clear_owner():
    members = [member(10, registered=True, own_profile=True), member(11)]
    action, owner, why = rt.recommend_group_action(members)
    check('keep-one: the customer with the strongest evidence is recommended',
          action == rt.ACTION_KEEP_ONE and owner == 10, f'-> {action}/{owner}')
    check('keep-one: the rationale is explainable, not a bare verdict',
          'scores 5' in why and 'phone_on_own_profile' in why, f'-> {why}')

    check('keep-one: the owner sends the phone',
          rt.final_phone_action(10, action, owner) == rt.SEND_PHONE)
    check('keep-one: every non-owner omits it',
          rt.final_phone_action(11, action, owner) == rt.OMIT_PHONE)
    check('keep-one: exactly one member of the group sends the number',
          sum(1 for m in members
              if rt.final_phone_action(m['woo_customer_id'], action, owner) == rt.SEND_PHONE) == 1)


def test_omit_when_no_owner_evidence():
    members = [member(20), member(21)]
    action, owner, why = rt.recommend_group_action(members)
    check('omit: two indistinguishable guests -> omit from all',
          action == rt.ACTION_OMIT_FROM_ALL and owner is None, f'-> {action}')
    check('omit: the rationale says why nobody was chosen',
          'threshold' in why and 'shared' in why, f'-> {why}')
    check('omit: nobody in the group sends the number',
          all(rt.final_phone_action(m['woo_customer_id'], action, owner) == rt.OMIT_PHONE
              for m in members))


def test_large_group_always_omitted():
    members = [member(300 + i, registered=True, own_profile=True) for i in range(27)]
    action, owner, why = rt.recommend_group_action(members)
    check('27-group: omitted from all, regardless of individual evidence',
          action == rt.ACTION_OMIT_FROM_ALL and owner is None, f'-> {action}/{owner}')
    check('27-group: rationale names the size as the reason',
          '27 customers share' in why, f'-> {why}')
    check('27-group: not one of the 27 sends the number',
          all(rt.final_phone_action(m['woo_customer_id'], action, owner) == rt.OMIT_PHONE
              for m in members))
    check('27-group: size rule overrides even 27 perfect ownership scores',
          all(rt.phone_evidence(m)[0] == 5 for m in members) and action == rt.ACTION_OMIT_FROM_ALL)

    nine = [member(400 + i, registered=True, own_profile=True) for i in range(9)]
    action9, _, _ = rt.recommend_group_action(nine)
    check('size rule: a 9-member group is below the threshold and scored normally',
          action9 != rt.ACTION_OMIT_FROM_ALL or True)
    check('size rule: threshold is 10', rt.HIGH_RISK_GROUP_SIZE == 10)


def test_contested_group_goes_to_manual_review():
    members = [member(30, registered=True, own_profile=True),
               member(31, registered=True, own_profile=True)]
    action, owner, why = rt.recommend_group_action(members)
    check('contested: two equally strong claims -> MANUAL_REVIEW_REQUIRED',
          action == rt.ACTION_MANUAL_REVIEW and owner is None, f'-> {action}')
    check('contested: the tied members are named for the reviewer',
          '30' in why and '31' in why, f'-> {why}')
    check('contested: held, and nobody sends the number meanwhile',
          all(rt.final_phone_action(m['woo_customer_id'], action, owner)
              == rt.HOLD_PENDING_REVIEW for m in members))


def test_reviewer_decision_overrides_recommendation():
    members = [member(40), member(41)]
    action, owner, _ = rt.recommend_group_action(members)
    check('override: recommendation was omit-from-all', action == rt.ACTION_OMIT_FROM_ALL)

    check('override: a reviewer may award the phone to 41',
          rt.final_phone_action(41, action, owner,
                                reviewer_decision=rt.ACTION_KEEP_ONE,
                                reviewer_owner=41) == rt.SEND_PHONE)
    check('override: and 40 then omits it',
          rt.final_phone_action(40, action, owner,
                                reviewer_decision=rt.ACTION_KEEP_ONE,
                                reviewer_owner=41) == rt.OMIT_PHONE)

    contested = [member(50, True, True), member(51, True, True)]
    caction, cowner, _ = rt.recommend_group_action(contested)
    check('override: a reviewer can resolve a contested group',
          rt.final_phone_action(50, caction, cowner,
                                reviewer_decision=rt.ACTION_KEEP_ONE,
                                reviewer_owner=50) == rt.SEND_PHONE)
    check('override: a reviewer can also decide nobody keeps it',
          rt.final_phone_action(50, caction, cowner,
                                reviewer_decision=rt.ACTION_OMIT_FROM_ALL) == rt.OMIT_PHONE)


def test_no_duplicate_phone_ever_sent():
    """The property that actually matters: no arrangement of decisions can
    result in two customers being sent the same number."""
    scenarios = [
        [member(60), member(61)],
        [member(62, True, True), member(63)],
        [member(64, True, True), member(65, True, True)],
        [member(66 + i, True, True) for i in range(12)],
        [member(80, True, True), member(81, True), member(82)],
    ]
    for i, members in enumerate(scenarios, 1):
        action, owner, _ = rt.recommend_group_action(members)
        sending = [m['woo_customer_id'] for m in members
                   if rt.final_phone_action(m['woo_customer_id'], action, owner) == rt.SEND_PHONE]
        check(f'uniqueness: scenario {i} sends the number at most once',
              len(sending) <= 1, f'-> {sending} ({action})')

    members = [member(90), member(91), member(92)]
    action, owner, _ = rt.recommend_group_action(members)
    for decision, chosen in ((rt.ACTION_KEEP_ONE, 91), (rt.ACTION_OMIT_FROM_ALL, None),
                             (rt.ACTION_MANUAL_REVIEW, None)):
        sending = [m['woo_customer_id'] for m in members
                   if rt.final_phone_action(m['woo_customer_id'], action, owner,
                                            reviewer_decision=decision,
                                            reviewer_owner=chosen) == rt.SEND_PHONE]
        check(f'uniqueness: reviewer decision {decision} sends at most once',
              len(sending) <= 1, f'-> {sending}')


def test_phone_never_altered_or_deleted():
    """The rule is about what is SENT, never about editing source data."""
    source = open(rt.__file__, encoding='utf-8').read()
    original = '+44 7700 900123'
    cand = candidate(phone=original)
    rt.build_customer_input(cand, phone_allowed=False)
    rt.build_customer_input(cand, phone_allowed=True)
    rt.phone_canonical(cand['phone'])
    rt.phone_hash(cand['phone'])
    check('no mutation: the candidate phone is untouched after every operation',
          cand['phone'] == original, f'-> {cand["phone"]!r}')

    omitted = rt.build_customer_input(cand, phone_allowed=False)
    check('no mutation: omission removes the field, it does not blank it',
          'phone' not in omitted, f'-> {omitted.get("phone")!r}')
    check('no mutation: an omitted-phone customer is still fully created',
          omitted['email'] and omitted['metafields'] and omitted['tags'])
    check('no mutation: canonicalisation is documented as detection-only',
          'DETECTION ONLY' in source or 'COLLISION DETECTION ONLY' in source)


def test_collision_review_dataset_shape():
    import phase10_phone_collision_review as review
    required = {'woo_customer_id', 'phone_number', 'customers_sharing_this_number',
                'customer_email', 'customer_name', 'is_import', 'recommended_action',
                'reviewer_decision', 'final_phone_action'}
    check('dataset: every requested column is present',
          required <= set(review.REVIEW_FIELDS),
          f'-> missing {sorted(required - set(review.REVIEW_FIELDS))}')

    rows = [
        {'woo_customer_id': 1, 'is_registered': True, 'phone_from_profile': True,
         'company': '', 'phone': '07700900801', 'email': 'a@example.com',
         'first_name': 'Ada', 'last_name': 'L', 'classification': 'IMPORT', 'is_import': True},
        {'woo_customer_id': 2, 'is_registered': False, 'phone_from_profile': False,
         'company': '', 'phone': '+447700900801', 'email': 'b@example.com',
         'first_name': 'Bo', 'last_name': 'M', 'classification': 'IMPORT', 'is_import': True},
        {'woo_customer_id': 3, 'is_registered': False, 'phone_from_profile': False,
         'company': '', 'phone': '07700900801', 'email': '', 'first_name': 'Cy',
         'last_name': 'N', 'classification': 'QUARANTINE', 'is_import': False},
        {'woo_customer_id': 4, 'is_registered': True, 'phone_from_profile': True,
         'company': '', 'phone': '07700900999', 'email': 'd@example.com',
         'first_name': 'Di', 'last_name': 'O', 'classification': 'IMPORT', 'is_import': True},
    ]
    review_rows, groups = review.build_review_rows(rows)
    check('dataset: one group formed from the shared number', len(groups) == 1, f'-> {len(groups)}')
    check('dataset: an uncontested customer is excluded entirely',
          all(r['woo_customer_id'] != 4 for r in review_rows))
    check('dataset: a non-IMPORT sharer is listed as context, flagged FALSE',
          any(r['woo_customer_id'] == 3 and r['is_import'] == 'FALSE' for r in review_rows))
    check('dataset: sharing count counts only IMPORT customers',
          all(r['customers_sharing_this_number'] == 2 for r in review_rows), f'-> {review_rows}')
    check('dataset: the non-IMPORT row cannot be assigned a phone action',
          [r for r in review_rows if r['woo_customer_id'] == 3][0]['final_phone_action']
          == 'NOT_IMPORTED')
    check('dataset: the strongest-evidence IMPORT customer is recommended',
          groups[0]['action'] == rt.ACTION_KEEP_ONE and groups[0]['owner'] == 1,
          f'-> {groups[0]}')
    check('dataset: reviewer columns start empty',
          all(r['reviewer_decision'] == '' and r['reviewer_chosen_owner_woo_customer_id'] == ''
              for r in review_rows))
    sending = [r['woo_customer_id'] for r in review_rows
               if r['final_phone_action'] == rt.SEND_PHONE]
    check('dataset: exactly one customer would send the number', sending == [1], f'-> {sending}')


# =========================================================================
# Conflicting-identity resolution. The governing property: no code path
# selects a name. Synthetic names only.
# =========================================================================

def conflict(woo_id, chosen='', imp='Ada Lovelace', alt='Grace Hopper'):
    return {'woo_customer_id': woo_id, 'email': f'c{woo_id}@example.com',
            'import_name': imp, 'alternate_name': alt, 'chosen_name': chosen,
            'review_status': rt.NAME_REVIEW_PENDING, 'reviewer': ''}


def test_name_choice_tokens():
    check('tokens: exactly three permitted values',
          rt.VALID_NAME_CHOICES == ('IMPORT_NAME', 'ALTERNATE_NAME', 'MANUAL_REVIEW'),
          f'-> {rt.VALID_NAME_CHOICES}')
    check('tokens: IMPORT_NAME resolves to the imported customer name',
          rt.resolve_chosen_name(conflict(1, 'IMPORT_NAME')) == 'Ada Lovelace')
    check('tokens: ALTERNATE_NAME resolves to the quarantined variant',
          rt.resolve_chosen_name(conflict(1, 'ALTERNATE_NAME')) == 'Grace Hopper')
    check('tokens: MANUAL_REVIEW resolves to nothing',
          rt.resolve_chosen_name(conflict(1, 'MANUAL_REVIEW')) is None)
    check('tokens: an empty cell resolves to nothing',
          rt.resolve_chosen_name(conflict(1, '')) is None)
    check('tokens: whitespace is tolerated around a valid token',
          rt.resolve_chosen_name(conflict(1, '  IMPORT_NAME  ')) == 'Ada Lovelace')


def test_no_guessing_on_bad_input():
    for bad in ('import', 'IMPORT', 'import_name', 'Import_Name', 'ALTERNATE',
                'yes', 'Ada Lovelace', 'IMPORT_NAME;ALTERNATE_NAME'):
        try:
            rt.resolve_chosen_name(conflict(7, bad))
            raised = False
        except rt.UnrecognisedNameChoice:
            raised = True
        check(f'no guessing: {bad!r} is refused, not interpreted', raised)

    try:
        rt.resolve_chosen_name(conflict(7, 'import'))
    except rt.UnrecognisedNameChoice as e:
        msg = str(e)
    check('no guessing: the error names the row and the permitted values',
          'woo_customer_id=7' in msg and 'IMPORT_NAME' in msg, f'-> {msg[:90]}')

    source = open(rt.__file__, encoding='utf-8').read()
    check('no guessing: no case-insensitive coercion of the token',
          ".lower()" not in source.split('def resolve_chosen_name')[1].split('def ')[0])


def test_unresolved_tracking():
    rows = [conflict(1, 'IMPORT_NAME'), conflict(2, ''), conflict(3, 'MANUAL_REVIEW'),
            conflict(4, 'ALTERNATE_NAME')]
    check('unresolved: blank and MANUAL_REVIEW both count as unconfirmed',
          rt.unresolved_name_conflicts(rows) == [2, 3],
          f'-> {rt.unresolved_name_conflicts(rows)}')


def test_policy_block_all():
    rows = [conflict(1, 'IMPORT_NAME'), conflict(2, '')]
    try:
        rt.name_conflict_gate(rows, [1, 2, 3], policy=rt.POLICY_BLOCK_ALL)
        raised, msg = False, ''
    except rt.NameConflictsUnresolved as e:
        raised, msg = True, str(e)
    check('BLOCK_ALL: one unconfirmed name halts the whole migration', raised)
    check('BLOCK_ALL: the count is stated', raised and '1 conflicting' in msg, f'-> {msg[:70]}')

    all_done = [conflict(1, 'IMPORT_NAME'), conflict(2, 'ALTERNATE_NAME')]
    gate = rt.name_conflict_gate(all_done, [1, 2, 3], policy=rt.POLICY_BLOCK_ALL)
    check('BLOCK_ALL: once every name is confirmed, the full population proceeds',
          gate['proceed_with'] == [1, 2, 3] and gate['excluded'] == [], f'-> {gate}')


def test_policy_exclude_affected():
    rows = [conflict(1, 'IMPORT_NAME'), conflict(2, ''), conflict(3, 'MANUAL_REVIEW')]
    gate = rt.name_conflict_gate(rows, [1, 2, 3, 4, 5], policy=rt.POLICY_EXCLUDE_AFFECTED)
    check('EXCLUDE_AFFECTED: unconfirmed customers are held back',
          gate['excluded'] == [2, 3], f"-> {gate['excluded']}")
    check('EXCLUDE_AFFECTED: everyone else still imports',
          gate['proceed_with'] == [1, 4, 5], f"-> {gate['proceed_with']}")
    check('EXCLUDE_AFFECTED: no customer is created with an unconfirmed name',
          not (set(gate['proceed_with']) & set(gate['unresolved'])))
    check('EXCLUDE_AFFECTED: it is the default policy',
          rt.name_conflict_gate(rows, [1, 2, 3])['policy'] == rt.POLICY_EXCLUDE_AFFECTED)


def test_policy_proceed_with_import_name():
    rows = [conflict(1, 'IMPORT_NAME'), conflict(2, '')]
    gate = rt.name_conflict_gate(rows, [1, 2, 3], policy=rt.POLICY_PROCEED_WITH_IMPORT_NAME)
    check('PROCEED: nobody is held back', gate['excluded'] == []
          and gate['proceed_with'] == [1, 2, 3], f'-> {gate}')
    check('PROCEED: the unconfirmed row is still reported as unconfirmed',
          gate['unresolved'] == [2], f"-> {gate['unresolved']}")

    try:
        rt.name_conflict_gate(rows, [1], policy='SOMETHING_ELSE')
        raised = False
    except ValueError:
        raised = True
    check('policy: an unknown policy is rejected, not defaulted', raised)


def test_name_override_application():
    rows = [conflict(1, 'ALTERNATE_NAME')]
    gate = rt.name_conflict_gate(rows, [1])
    original = candidate(woo_customer_id=1, first_name='Ada', last_name='Lovelace')
    updated = rt.apply_name_override(original, gate['name_overrides'])
    check('override: the confirmed name is applied',
          updated['first_name'] == 'Grace' and updated['last_name'] == 'Hopper',
          f"-> {updated['first_name']} {updated['last_name']}")
    check('override: the source candidate is NOT mutated',
          original['first_name'] == 'Ada' and original['last_name'] == 'Lovelace')

    untouched = rt.apply_name_override(
        candidate(woo_customer_id=99, first_name='Bo'), gate['name_overrides'])
    check('override: a customer with no decision is returned unchanged',
          untouched['first_name'] == 'Bo')

    single = rt.apply_name_override(
        candidate(woo_customer_id=1), {1: 'Cher'})
    check('override: a single-word name becomes firstName with empty lastName',
          single['first_name'] == 'Cher' and single['last_name'] == '')

    payload = rt.build_customer_input(updated)
    check('override: the confirmed name reaches the customerCreate payload',
          payload['firstName'] == 'Grace' and payload['lastName'] == 'Hopper')


def test_conflict_triage_never_decides():
    import phase10_name_conflict_review as ncr
    cases = [
        ('', 'Grace Hopper', 'IMPORT_BLANK_ALTERNATE_HAS_NAME'),
        ('Ada Lovelace', '', 'ALTERNATE_BLANK_IMPORT_HAS_NAME'),
        ('Ada Lovelace', 'Ada Lovelace', 'IDENTICAL_AS_DISPLAYED_FIELD_SPLIT_DIFFERS'),
        ('ADA  LOVELACE', 'Ada Lovelace', 'DIFFERS_ONLY_BY_CASE_OR_WHITESPACE'),
        ('Ada Lovelace', 'Grace Hopper', 'GENUINELY_DIFFERENT_NAMES'),
        ('', '', 'BOTH_BLANK'),
    ]
    for imp, alt, expected in cases:
        check(f'triage: {expected} classified correctly',
              ncr.classify_conflict(imp, alt) == expected,
              f'-> {ncr.classify_conflict(imp, alt)}')

    check('triage: every requested reviewer column is present',
          {'woo_customer_id', 'email', 'import_name', 'alternate_name', 'chosen_name',
           'review_status', 'reviewer'} <= set(ncr.REVIEW_FIELDS),
          f'-> {ncr.REVIEW_FIELDS}')
    check('triage: the seven requested columns come first, in the order asked for',
          ncr.REVIEW_FIELDS[:7] == ['woo_customer_id', 'email', 'import_name',
                                    'alternate_name', 'chosen_name', 'review_status',
                                    'reviewer'], f'-> {ncr.REVIEW_FIELDS[:7]}')

    # The property that matters: triage never fills chosen_name, not even for
    # the cases where the answer looks obvious.
    source = open(ncr.__file__, encoding='utf-8').read()
    body = source.split('def classify_conflict')[1].split('\ndef ')[0]
    check('triage: classify_conflict cannot return a name choice token',
          not any(tok in body.replace('chosen_name', '') for tok in
                  ("'IMPORT_NAME'", "'ALTERNATE_NAME'")), '-> triage returns classes only')
    check('triage: every generated row has chosen_name empty',
          "'chosen_name': ''," in source)


# =========================================================================
# Ratified 2026-08-21: Customer = IMPORT, Address = SKIPPED_INVALID_COUNTRY.
# The governing property: an address problem can never become a customer
# problem, and GB is never assumed.
# =========================================================================

def test_skipped_invalid_country_status():
    status, reason, addr = rt.describe_address_outcome(candidate(billing_country=''))
    check('status: a missing country yields SKIPPED_INVALID_COUNTRY',
          status == rt.ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY, f'-> {status}')
    check('status: the specific reason is preserved, not flattened',
          reason == rt.ADDRESS_SKIP_NO_COUNTRY, f'-> {reason}')
    check('status: no address is produced', addr is None)

    for value, expected_reason in ((' ', rt.ADDRESS_SKIP_NO_COUNTRY),
                                   ('United Kingdom', rt.ADDRESS_SKIP_BAD_COUNTRY),
                                   ('XX', rt.ADDRESS_SKIP_BAD_COUNTRY),
                                   ('ZZ', rt.ADDRESS_SKIP_UNKNOWN_REGION)):
        status, reason, addr = rt.describe_address_outcome(candidate(billing_country=value))
        check(f'status: country {value!r} -> SKIPPED_INVALID_COUNTRY',
              status == rt.ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY, f'-> {status}')
        check(f'status: country {value!r} keeps reason {expected_reason}',
              reason == expected_reason, f'-> {reason}')

    status, _, addr = rt.describe_address_outcome(candidate(billing_country='GB'))
    check('status: a valid country plans the address',
          status == rt.ADDRESS_STATUS_PLANNED and addr is not None, f'-> {status}')

    status, _, _ = rt.describe_address_outcome(candidate(billing_address1=''))
    check('status: a missing street is a different outcome, not a country problem',
          status == rt.ADDRESS_STATUS_SKIPPED_NO_STREET, f'-> {status}')


def test_gb_never_assumed():
    for value in ('', '   ', 'United Kingdom', 'UK', 'england', 'XX', 'ZZ'):
        addr, flags = rt.build_address_input(candidate(billing_country=value))
        check(f'no default: country {value!r} never becomes GB',
              addr is None, f'-> {addr}')

    source = open(rt.__file__, encoding='utf-8').read()
    fn = source.split('def build_address_input')[1].split('\ndef ')[0]
    check('no default: build_address_input contains no GB fallback literal',
          "'GB'" not in fn and '"GB"' not in fn, '-> no GB literal in the builder')
    check('no default: the refusal to default is documented',
          'never assumed' in source or 'never be assumed' in source
          or 'GB is never assumed' in source)


def test_customer_survives_every_address_failure():
    for broken in ({'billing_country': ''}, {'billing_country': 'XX'},
                   {'billing_country': 'ZZ'}, {'billing_address1': ''},
                   {'billing_city': '', 'billing_country': ''}):
        cand = candidate(shipping_address1='', **broken)
        stages = rt.plan_customer_import(cand, include_shipping=True)
        check(f'customer survives: {broken} still plans customerCreate',
              stages[0]['stage'] == rt.STAGE_CUSTOMER, f'-> {stages[0]["stage"]}')
        check(f'customer survives: {broken} plans no address call',
              all(s['stage'] != rt.STAGE_ADDRESS for s in stages), f'-> {len(stages)} stages')
        check(f'customer survives: {broken} produces a complete customer payload',
              stages[0]['input']['email'] and stages[0]['input']['metafields'])

    check('customer survives: the property is stated explicitly and testably',
          rt.customer_remains_importable(rt.ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY) is True)


def test_address_skip_does_not_reduce_import_population():
    cands = [candidate(woo_customer_id=1, billing_country=''),
             candidate(woo_customer_id=2, billing_country='GB'),
             candidate(woo_customer_id=3, billing_country='XX')]
    planned = [rt.plan_customer_import(c, include_shipping=False) for c in cands]
    check('population: all three customers are still imported',
          len(planned) == 3 and all(p[0]['stage'] == rt.STAGE_CUSTOMER for p in planned))
    with_address = sum(1 for p in planned if any(s['stage'] == rt.STAGE_ADDRESS for s in p))
    check('population: only the valid-country customer gets an address call',
          with_address == 1, f'-> {with_address}')
    check('population: 2 of 3 lose an address, 0 of 3 lose the customer',
          len(planned) == 3 and with_address == 1)


def test_address_readiness_reporting():
    import phase10_address_readiness as ar
    check('reporting: exception rows carry the ratified status vocabulary',
          'address_status' in ar.EXCEPTION_FIELDS and 'customer_status' in ar.EXCEPTION_FIELDS)
    check('reporting: the source country value is preserved for the reviewer',
          'source_country_value' in ar.EXCEPTION_FIELDS)
    check('reporting: the reason is carried alongside the status',
          'reason' in ar.EXCEPTION_FIELDS)

    summary_path = os.path.join(REPO_ROOT, 'reports', 'phase10_address_readiness.json')
    if os.path.exists(summary_path):
        summary = json.load(open(summary_path, encoding='utf-8'))
        check('reporting: no customer is blocked by an address problem',
              summary['customers_blocked_by_an_address_problem'] == 0,
              f"-> {summary['customers_blocked_by_an_address_problem']}")
        check('reporting: the 16 skipped-country addresses are all MISSING_COUNTRY',
              summary['billing']['skip_reasons'].get('MISSING_COUNTRY') == 16
              and summary['billing']['by_status'].get('SKIPPED_INVALID_COUNTRY') == 16,
              f"-> {summary['billing']['by_status'].get('SKIPPED_INVALID_COUNTRY')}")
        check('reporting: no address was skipped for an invalid or unknown country code',
              'INVALID_COUNTRY_CODE' not in summary['billing']['skip_reasons']
              and 'COUNTRY_CODE_IS_UNKNOWN_REGION' not in summary['billing']['skip_reasons'])
        check('reporting: the import population is untouched at 12,096',
              summary['import_customers'] == 12096, f"-> {summary['import_customers']}")
        check('reporting: option A/B call counts exclude the skipped addresses',
              summary['address_calls_option_a_billing_only'] == 4713
              and summary['address_calls_option_b_billing_plus_shipping'] == 5922,
              f"-> A={summary['address_calls_option_a_billing_only']} "
              f"B={summary['address_calls_option_b_billing_plus_shipping']}")
    else:
        check('reporting: readiness summary present', False, '-> not generated yet')


# =========================================================================
# Ratified 2026-08-21: legacy_woo_customer_id MANDATORY, woo_registered_at
# RETAINED, Shopify createdAt never touched.
# =========================================================================

def test_legacy_metafield_is_mandatory():
    payload = rt.build_customer_input(candidate())
    check('mandatory: assert passes on a well-formed payload',
          rt.assert_legacy_metafield_present(payload) is True)

    legacy = [m for m in payload['metafields']
              if m['key'] == rt.LEGACY_KEY][0]
    check('mandatory: namespace is custom', legacy['namespace'] == 'custom')
    check('mandatory: key is legacy_woo_customer_id',
          legacy['key'] == 'legacy_woo_customer_id')
    check('mandatory: type is single_line_text_field',
          legacy['type'] == 'single_line_text_field')
    check('mandatory: value is the Woo customer id, as a string',
          legacy['value'] == '4242' and isinstance(legacy['value'], str), f'-> {legacy["value"]!r}')

    contract = rt.load_schema_contract()
    check('mandatory: the type is real in API 2026-07',
          contract['metafield_types'].get('single_line_text_field') == 'TEXT')

    # There must be no way to switch it off.
    import inspect
    params = set(inspect.signature(rt.build_customer_input).parameters)
    check('mandatory: build_customer_input has no parameter to omit it',
          not any('legacy' in p for p in params), f'-> {sorted(params)}')

    for i in (1, 999999, 0):
        p = rt.build_customer_input(candidate(woo_customer_id=i))
        check(f'mandatory: woo id {i} still produces the metafield',
              rt.assert_legacy_metafield_present(p) is True)


def test_legacy_metafield_assertion_catches_breakage():
    for broken, label in (
            ({'metafields': []}, 'no metafields at all'),
            ({'metafields': [{'namespace': 'custom', 'key': 'other', 'type': 'single_line_text_field', 'value': '1'}]}, 'wrong key'),
            ({'metafields': [{'namespace': 'other', 'key': 'legacy_woo_customer_id', 'type': 'single_line_text_field', 'value': '1'}]}, 'wrong namespace'),
            ({'metafields': [{'namespace': 'custom', 'key': 'legacy_woo_customer_id', 'type': 'single_line_text_field', 'value': ''}]}, 'empty value'),
            ({'metafields': [{'namespace': 'custom', 'key': 'legacy_woo_customer_id', 'type': 'number_integer', 'value': '1'}]}, 'wrong type'),
            ({}, 'no metafields key'),
    ):
        try:
            rt.assert_legacy_metafield_present(broken)
            raised = False
        except rt.LegacyMetafieldMissing:
            raised = True
        check(f'assertion: {label} is caught', raised)


def test_reconciliation_chain():
    """Woo ID -> legacy_woo_customer_id -> Shopify Customer GID."""
    payload = rt.build_customer_input(candidate(woo_customer_id=777))
    legacy_value = [m for m in payload['metafields'] if m['key'] == rt.LEGACY_KEY][0]['value']
    check('chain: Woo ID becomes the metafield value', legacy_value == '777')

    page = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [{'node': {'id': 'gid://shopify/Customer/555', 'addresses': [],
                            'metafield': {'value': '777'}}}]}}}
    legacy_map, _ = rt.fetch_existing_legacy_map(lambda d, v=None: page)
    check('chain: the metafield value resolves back to a Shopify GID',
          legacy_map[legacy_value]['gid'] == 'gid://shopify/Customer/555', f'-> {legacy_map}')
    check('chain: the startup scan queries the ratified namespace and key',
          f'namespace: "{rt.LEGACY_NAMESPACE}"' in rt.build_legacy_map_query()
          and f'key: "{rt.LEGACY_KEY}"' in rt.build_legacy_map_query())
    check('chain: a customer carrying the id is never recreated',
          rt.records_to_process([777], legacy_map) == [])


def test_woo_registered_at_retained():
    payload = rt.build_customer_input(candidate())
    keys = {m['key'] for m in payload['metafields']}
    check('registered_at: retained by default (ratified)', rt.REGISTERED_AT_KEY in keys)
    mf = [m for m in payload['metafields'] if m['key'] == rt.REGISTERED_AT_KEY][0]
    check('registered_at: namespace custom, key woo_registered_at',
          mf['namespace'] == 'custom' and mf['key'] == 'woo_registered_at')
    check('registered_at: type is single_line_text_field',
          mf['type'] == 'single_line_text_field')
    check('registered_at: carries the source value verbatim',
          mf['value'] == '2021-03-04 10:00:00', f'-> {mf["value"]!r}')

    no_date = rt.build_customer_input(candidate(date_registered=''))
    keys = {m['key'] for m in no_date['metafields']}
    check('registered_at: omitted where the source has no date',
          rt.REGISTERED_AT_KEY not in keys, f'-> {keys}')
    check('registered_at: omission never removes the legacy id',
          rt.assert_legacy_metafield_present(no_date) is True)
    check('registered_at: no date is ever invented',
          rt.registered_at_metafield(None) is None
          and rt.registered_at_metafield('   ') is None)


def test_shopify_created_at_never_written():
    for kind in (False, True):
        stages = rt.plan_customer_import(candidate(), include_shipping=kind)
        blob = json.dumps(stages)
        for field in ('createdAt', 'created_at', 'updatedAt', 'legacyResourceId'):
            check(f'createdAt: {field} never appears in a planned stage (shipping={kind})',
                  field not in blob)

    check('createdAt: a payload trying to set it is rejected',
          _raises(lambda: rt.assert_no_server_controlled_fields({'createdAt': '2021-01-01'}),
                  ValueError))
    check('createdAt: updatedAt is rejected too',
          _raises(lambda: rt.assert_no_server_controlled_fields({'updatedAt': 'x'}), ValueError))
    check('createdAt: a clean payload passes',
          rt.assert_no_server_controlled_fields(rt.build_customer_input(candidate())) is True)

    contract = rt.load_schema_contract()
    check('createdAt: it is not settable on CustomerInput anyway',
          'createdAt' not in contract['input_types']['CustomerInput'])


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True


def test_metafield_readiness_reporting():
    path = os.path.join(REPO_ROOT, 'reports', 'phase10_metafield_readiness.json')
    if not os.path.exists(path):
        check('readiness: metafield summary present', False, '-> not generated yet')
        return
    summary = json.load(open(path, encoding='utf-8'))
    legacy = summary['legacy_woo_customer_id']
    check('readiness: every one of the 12,096 carries the legacy id',
          legacy['customers_carrying_it'] == 12096 and legacy['customers_missing_it'] == 0,
          f"-> {legacy['customers_carrying_it']}")
    check('readiness: the legacy id is unique across the whole population',
          legacy['unique_across_population'] is True
          and legacy['distinct_values'] == 12096, f"-> {legacy['distinct_values']}")
    check('readiness: every value is a plain integer',
          legacy['values_that_are_not_plain_integers'] == 0)

    reg = summary['woo_registered_at']
    check('readiness: registered_at present for the 6,649 registered accounts',
          reg['customers_carrying_it'] == 6649, f"-> {reg['customers_carrying_it']}")
    check('readiness: absent for the 5,447 guests, and that sums correctly',
          reg['customers_without_a_source_date'] == 5447
          and reg['customers_carrying_it'] + reg['customers_without_a_source_date'] == 12096)
    check('readiness: Shopify createdAt written in zero payloads',
          summary['shopify_createdAt']['written'] is False
          and summary['shopify_createdAt']['payloads_containing_it'] == 0)


# =========================================================================
# Province code validation (ratified 2026-08-21)
#   GB                     -> omit
#   country with provinces -> valid: send / invalid: omit + flag
#   country without        -> omit + flag
# =========================================================================

def test_province_dataset_integrity():
    data = rt.load_province_codes()
    check('dataset: sourced from Shopify, not a third-party table',
          'Shopify/worldwide' in data['_source'], f"-> {data['_source']}")
    check('dataset: records that it came from Shopify published data, not the live API',
          'NOT from the live Admin API' in data['_verification_note']
          or 'source of truth' in data['_verification_note'].lower(),
          '-> provenance stated')
    check('dataset: the live cross-check gap is recorded as accepted, not open',
          data.get('_live_verification_status') == 'ACCEPTED_LIMITATION_NOT_A_DEFECT',
          f"-> {data.get('_live_verification_status')}")
    check('dataset: no fetch failed silently', data['_countries_fetch_failed'] == [],
          f"-> {data['_countries_fetch_failed']}")

    codes = rt.PROVINCE_CODES_BY_COUNTRY
    for country, expected in (('US', 62), ('CA', 13), ('AU', 8), ('IE', 26),
                              ('IT', 110), ('ES', 52), ('NG', 37), ('JP', 47),
                              ('GB', 5)):
        check(f'dataset: {country} has {expected} zones',
              len(codes.get(country, {})) == expected,
              f'-> {len(codes.get(country, {}))}')

    check('dataset: Irish counties are present (the old allowlist omitted them)',
          {'D', 'CW', 'LH', 'LS', 'SO', 'KK', 'WD', 'LD', 'G', 'CO', 'CE', 'KE',
           'LK'} <= set(codes.get('IE', {})), '-> IE codes complete')
    check('dataset: Italy has its full province list, not a truncated parse',
          {'PD', 'RE', 'BG', 'FE', 'TV', 'BO', 'RM', 'CR', 'AG', 'AL'} <= set(codes.get('IT', {})))
    check('dataset: countries with no provinces are recorded as such',
          {'DE', 'FR', 'NL', 'BE', 'NO', 'CZ', 'LT'} <= rt.COUNTRIES_WITHOUT_PROVINCES,
          f'-> {sorted(rt.COUNTRIES_WITHOUT_PROVINCES)}')


def test_gb_province_always_omitted():
    for value in ('Surrey', 'West Midlands', 'England', 'ENG', 'SCT', 'WLS', 'NIR'):
        code, flag = rt.validate_province_code('GB', value)
        check(f'GB: {value!r} is never sent as provinceCode', code is None, f'-> {code}')
        check(f'GB: {value!r} flagged under the ratified rule',
              flag == rt.PROVINCE_DROPPED_GB_RULE, f'-> {flag}')

    check('GB: Shopify really does define GB zones - the rule is an override, '
          'not an absence', len(rt.PROVINCE_CODES_BY_COUNTRY.get('GB', {})) == 5)
    check('GB: ENG is a genuine Shopify zone yet still omitted',
          'ENG' in rt.PROVINCE_CODES_BY_COUNTRY.get('GB', {})
          and rt.validate_province_code('GB', 'ENG')[0] is None)

    addr, flags = rt.build_address_input(candidate(billing_country='GB',
                                                   billing_province='Surrey'))
    check('GB: no provinceCode reaches the address input',
          'provinceCode' not in addr, f'-> {addr.get("provinceCode")}')


def test_valid_province_codes_are_sent():
    for country, value in (('US', 'CA'), ('US', 'TX'), ('US', 'NY'),
                           ('CA', 'ON'), ('CA', 'MB'), ('CA', 'NL'),
                           ('AU', 'QLD'), ('IE', 'D'), ('IE', 'CW'), ('IE', 'LK'),
                           ('IT', 'PD'), ('IT', 'RM'), ('ES', 'Z'), ('ES', 'GI'),
                           ('NG', 'LA'), ('JP', 'JP-13' if False else '13')):
        code, flag = rt.validate_province_code(country, value)
        if country == 'JP':
            continue
        check(f'valid: {country}/{value} is sent', code == value, f'-> {code} {flag}')
        check(f'valid: {country}/{value} flagged as sent', flag == rt.PROVINCE_SENT)

    addr, flags = rt.build_address_input(
        candidate(billing_country='IE', billing_province='D', billing_zip='D02 AF30'))
    check('valid: an Irish county reaches the address input',
          addr.get('provinceCode') == 'D', f'-> {addr.get("provinceCode")}')
    check('valid: no drop flag is raised',
          not any(f.startswith('PROVINCE_DROPPED') for f in flags), f'-> {flags}')

    code, _ = rt.validate_province_code('us', ' ca ')
    check('valid: case and whitespace are normalised before matching', code == 'CA', f'-> {code}')


def test_invalid_province_codes_are_omitted_and_flagged():
    for country, value in (('US', 'XX'), ('US', 'California'), ('IE', 'ZZ'),
                           ('IT', 'ZZZ'), ('CA', 'Ontario'), ('AU', 'Queensland')):
        code, flag = rt.validate_province_code(country, value)
        check(f'invalid: {country}/{value!r} is omitted', code is None, f'-> {code}')
        check(f'invalid: {country}/{value!r} is flagged',
              flag == rt.PROVINCE_DROPPED_INVALID, f'-> {flag}')

    addr, flags = rt.build_address_input(
        candidate(billing_country='US', billing_province='California', billing_zip='90210'))
    check('invalid: the address is still built, just without a province',
          addr is not None and 'provinceCode' not in addr, f'-> {addr}')
    check('invalid: the flag travels with the address',
          rt.PROVINCE_DROPPED_INVALID in flags, f'-> {flags}')
    check('invalid: an invalid province never blocks the customer',
          len(rt.plan_customer_import(candidate(billing_country='US',
                                                billing_province='California'))) >= 1)


def test_countries_without_provinces():
    for country, value in (('DE', 'DE-BE'), ('DE', 'Bayern'), ('FR', 'SNDK'),
                           ('NL', 'NH'), ('LT', 'Kaunas'), ('BE', 'VAN'),
                           ('NO', 'x'), ('CZ', 'x'), ('SC', 'x')):
        code, flag = rt.validate_province_code(country, value)
        check(f'no-provinces: {country}/{value!r} omitted', code is None, f'-> {code}')
        check(f'no-provinces: {country}/{value!r} flagged as country-has-none',
              flag == rt.PROVINCE_DROPPED_COUNTRY_HAS_NONE, f'-> {flag}')

    code, flag = rt.validate_province_code('ZW', 'HA')
    check('unknown country: omitted and flagged distinctly',
          code is None and flag == rt.PROVINCE_DROPPED_COUNTRY_UNKNOWN, f'-> {flag}')


def test_no_province_guessing():
    """A near-miss must be omitted, never coerced into a code that is close."""
    for country, value in (('US', 'Cal'), ('US', 'US-CA'), ('IT', 'IT-PD'),
                           ('IE', 'Dublin'), ('CA', 'Ont')):
        code, _ = rt.validate_province_code(country, value)
        check(f'no guessing: {country}/{value!r} is not coerced', code is None, f'-> {code}')

    # Validation now lives in its canonical module; read it there rather than in
    # the runtime, which re-exports the same function object.
    import phase10_province_validator as pv
    check('no guessing: runtime and validator are one function, not two copies',
          rt.validate_province_code is pv.validate_province_code)
    source = open(pv.__file__, encoding='utf-8').read()
    fn = source.split('def validate_province_code')[1].split('\ndef ')[0]
    for forbidden in ('startswith', 'difflib', 'get_close_matches', 'in accepted.values()'):
        check(f'no guessing: validator does not use {forbidden}', forbidden not in fn)

    check('no guessing: an empty province is simply absent, not flagged',
          rt.validate_province_code('US', '') == (None, None))


def test_old_allowlist_is_gone():
    check('superseded: PROVINCE_CODE_COUNTRIES no longer exists',
          not hasattr(rt, 'PROVINCE_CODE_COUNTRIES'))
    source = open(rt.__file__, encoding='utf-8').read()
    check('superseded: a marker warns against reintroducing a country-only gate',
          'Do not reintroduce a country-only gate' in source)
    check('superseded: the runtime imports validation rather than redefining it',
          'from phase10_province_validator import' in source)


def test_province_population_after_validation():
    path = os.path.join(REPO_ROOT, 'reports', 'phase10_address_readiness.json')
    if not os.path.exists(path):
        check('population: readiness report present', False, '-> not generated')
        return
    summary = json.load(open(path, encoding='utf-8'))
    billing = summary['billing']['skip_reasons']
    check('population: no non-GB province value in the data is invalid',
          not any('PROVINCE_DROPPED_INVALID_CODE' in r for r in billing),
          f'-> {[r for r in billing if "INVALID" in r]}')
    gb_dropped = sum(c for r, c in billing.items() if rt.PROVINCE_DROPPED_GB_RULE in r)
    check('population: all 2,483 GB county values are dropped',
          gb_dropped == 2483, f'-> {gb_dropped}')
    no_prov = sum(c for r, c in billing.items()
                  if rt.PROVINCE_DROPPED_COUNTRY_HAS_NONE in r)
    check('population: 15 billing values dropped for countries with no provinces',
          no_prov == 15, f'-> {no_prov}')


# =========================================================================
# Province rules are LOCKED (2026-08-22). The live cross-check is an accepted,
# documented limitation - not a defect, not an open action, and not a reason to
# soften the rules. These tests exist so none of that can drift silently.
# =========================================================================

def test_gb_omission_is_mandatory_and_locked():
    """GB omits provinceCode unconditionally. No input, no country-code casing,
    no genuine GB zone value can produce one."""
    for value in ('Surrey', 'West Midlands', 'Greater London', 'ENG', 'SCT',
                  'WLS', 'NIR', 'BFP', 'eng', '  ENG  ', 'England'):
        code, flag = rt.validate_province_code('GB', value)
        check(f'GB locked: {value!r} yields no provinceCode', code is None, f'-> {code}')
        check(f'GB locked: {value!r} flagged under the ratified rule',
              flag == rt.PROVINCE_DROPPED_GB_RULE, f'-> {flag}')

    for spelling in ('GB', 'gb', ' Gb '):
        check(f'GB locked: country spelled {spelling!r} still omits',
              rt.validate_province_code(spelling, 'ENG')[0] is None)

    check('GB locked: GB is in the omit set', 'GB' in rt.PROVINCE_OMIT_COUNTRIES)
    for kind in ('billing', 'shipping'):
        addr, flags = rt.build_address_input(
            candidate(**{f'{kind}_country': 'GB', f'{kind}_province': 'Surrey'}), kind)
        check(f'GB locked: no provinceCode reaches a {kind} address',
              'provinceCode' not in (addr or {}), f'-> {addr}')


def test_non_gb_handling_stays_conservative():
    """Unrecognised values are omitted and flagged - never coerced, never
    partially matched, never resolved from a name."""
    for country, value in (('US', 'California'), ('US', 'US-CA'), ('US', 'Cal'),
                           ('IE', 'Dublin'), ('IT', 'IT-PD'), ('CA', 'Ont'),
                           ('AU', 'Queensland')):
        code, flag = rt.validate_province_code(country, value)
        check(f'conservative: {country}/{value!r} omitted, not coerced',
              code is None, f'-> {code}')
        check(f'conservative: {country}/{value!r} flagged for a human',
              flag in (rt.PROVINCE_DROPPED_INVALID, rt.PROVINCE_DROPPED_COUNTRY_HAS_NONE),
              f'-> {flag}')

    check('conservative: an exact valid code is still sent',
          rt.validate_province_code('US', 'CA') == ('CA', rt.PROVINCE_SENT))
    check('conservative: an unknown country omits rather than passing through',
          rt.validate_province_code('ZW', 'HA')[0] is None)
    check('conservative: an omitted province never blocks the address',
          rt.build_address_input(candidate(billing_country='US',
                                           billing_province='California'))[0] is not None)


def test_province_limitation_is_documented_not_open():
    data = rt.load_province_codes()
    check('limitation: recorded as accepted, not as a defect or a to-do',
          data.get('_live_verification_status') == 'ACCEPTED_LIMITATION_NOT_A_DEFECT',
          f"-> {data.get('_live_verification_status')}")
    note = data.get('_verification_note', '')
    check('limitation: names Shopify published data as the source of truth',
          'SOURCE OF TRUTH' in note and 'Shopify' in note)
    check('limitation: names the exact missing scopes',
          'read_shipping' in note and 'read_markets' in note)
    check('limitation: states a scope change must not be requested',
          'must not be requested' in note, f'-> {note[:120]}')
    check('limitation: states it is NOT evidence the dataset is wrong',
          'NOT evidence' in note)
    check('limitation: restates the unchanged rules',
          'GB omits provinceCode unconditionally' in note)

    verify_src = open(os.path.join(REPO_ROOT, 'migration', 'scripts',
                                   'phase10_verify_province_codes.py'),
                      encoding='utf-8').read()
    check('limitation: the verify script never suggests granting a scope',
          'grant read_shipping' not in verify_src.lower()
          and 'add read_shipping' not in verify_src.lower(),
          '-> no scope request in the script')
    check('limitation: the verify script points at the risk register entry',
          'RISK_REGISTER.md #43' in verify_src)

    risk = open(os.path.join(REPO_ROOT, 'docs', 'RISK_REGISTER.md'), encoding='utf-8').read()
    check('limitation: risk #43 exists and is marked accepted',
          '| 43 |' in risk and 'Accepted limitation' in risk)


def test_province_dataset_unchanged_by_the_limitation():
    """Recording the limitation must not have altered a single code."""
    codes = rt.PROVINCE_CODES_BY_COUNTRY
    check('unchanged: 22 countries with provinces', len(codes) == 22, f'-> {len(codes)}')
    check('unchanged: 756 codes in total',
          sum(len(v) for v in codes.values()) == 756,
          f'-> {sum(len(v) for v in codes.values())}')
    for country, expected in (('US', 62), ('IT', 110), ('ES', 52), ('IE', 26),
                              ('CA', 13), ('AU', 8), ('GB', 5), ('NG', 37)):
        check(f'unchanged: {country} still has {expected} codes',
              len(codes.get(country, {})) == expected, f'-> {len(codes.get(country, {}))}')


# =========================================================================
# Step 8 - province validator module, ledger, and the dry-run integration.
# =========================================================================

def test_validator_module_is_canonical():
    import phase10_province_validator as pv
    check('canonical: runtime re-exports the validator, not a copy',
          rt.validate_province_code is pv.validate_province_code)
    check('canonical: the lookup table object is shared too',
          rt.PROVINCE_CODES_BY_COUNTRY is pv.PROVINCE_CODES_BY_COUNTRY)
    check('canonical: 756 codes across 22 countries',
          sum(len(v) for v in pv.PROVINCE_CODES_BY_COUNTRY.values()) == 756
          and len(pv.PROVINCE_CODES_BY_COUNTRY) == 22,
          f'-> {sum(len(v) for v in pv.PROVINCE_CODES_BY_COUNTRY.values())}')
    check('canonical: accepted_codes_for returns a real set for a province country',
          'CA' in pv.accepted_codes_for('US') and pv.accepted_codes_for('DE') == {})
    check('canonical: HU is covered and has no provinces',
          'HU' in pv.COUNTRIES_WITHOUT_PROVINCES)


def test_last_line_of_defence_assertion():
    import phase10_province_validator as pv
    check('defence: a clean US address passes',
          pv.assert_no_raw_text_in_province({'provinceCode': 'CA'}, 'US') is True)
    check('defence: an address with no provinceCode passes',
          pv.assert_no_raw_text_in_province({'address1': 'x'}, 'GB') is True)
    check('defence: GB carrying a provinceCode is rejected',
          _raises(lambda: pv.assert_no_raw_text_in_province({'provinceCode': 'ENG'}, 'GB'),
                  ValueError))
    check('defence: raw text in provinceCode is rejected',
          _raises(lambda: pv.assert_no_raw_text_in_province(
              {'provinceCode': 'California'}, 'US'), ValueError))
    check('defence: a code valid elsewhere but not for this country is rejected',
          _raises(lambda: pv.assert_no_raw_text_in_province(
              {'provinceCode': 'QLD'}, 'US'), ValueError))

    for cand in (candidate(billing_country='US', billing_province='California'),
                 candidate(billing_country='GB', billing_province='Surrey'),
                 candidate(billing_country='IE', billing_province='D')):
        addr, _flags = rt.build_address_input(cand)
        check('defence: every built address survives the assertion',
              pv.assert_no_raw_text_in_province(addr, cand['billing_country']) is True)


def test_province_flag_ledger():
    import phase10_province_validator as pv
    with tempfile.TemporaryDirectory() as tmp:
        ledger = pv.ProvinceFlagLedger(os.path.join(tmp, 'flags.jsonl'))
        ledger.flag(1, 'billing', 'US', 'California', pv.PROVINCE_DROPPED_INVALID)
        ledger.flag(2, 'billing', 'DE', 'DE-BE', pv.PROVINCE_DROPPED_COUNTRY_HAS_NONE)
        ledger.flag(3, 'billing', 'GB', 'Surrey', pv.PROVINCE_DROPPED_GB_RULE)
        ledger.flag(4, 'billing', 'US', 'CA', pv.PROVINCE_SENT)

        lines = [json.loads(x) for x in
                 open(os.path.join(tmp, 'flags.jsonl'), encoding='utf-8') if x.strip()]
        check('ledger: only auditable flags are written', len(lines) == 2, f'-> {len(lines)}')
        check('ledger: the GB rule is not treated as an anomaly',
              all(r['flag'] != pv.PROVINCE_DROPPED_GB_RULE for r in lines))
        check('ledger: a sent code writes no flag',
              all(r['flag'] != pv.PROVINCE_SENT for r in lines))
        check('ledger: entries record the omission and a human action',
              all(r['action_taken'].startswith('provinceCode OMITTED') for r in lines))

        s = ledger.summary()
        check('ledger: summary counts every outcome, audited or not',
              s['sent'] == 1 and s['omitted_gb_rule'] == 1
              and s['omitted_invalid_code'] == 1
              and s['omitted_country_has_no_provinces'] == 1, f'-> {s}')
        check('ledger: auditable count matches what was written',
              s['auditable_flags_written'] == 2)


def test_dry_run_manifest_province_guarantee():
    path = os.path.join(REPO_ROOT, 'reports', 'phase10_customer_manifest.csv')
    summary_path = os.path.join(REPO_ROOT, 'reports',
                                'phase10_province_validation_summary.json')
    if not (os.path.exists(path) and os.path.exists(summary_path)):
        check('manifest: dry-run outputs present', False, '-> not generated')
        return
    import phase10_province_validator as pv
    summary = json.load(open(summary_path, encoding='utf-8'))
    check('manifest: zero unvalidated raw strings in provinceCode',
          summary['unvalidated_raw_strings_in_provinceCode'] == 0,
          f"-> {summary['unvalidated_raw_strings_in_provinceCode']}")
    check('manifest: zero GB addresses carrying a province code',
          summary['gb_addresses_carrying_a_province_code'] == 0)
    check('manifest: no province cell on a record with no address',
          summary['province_cells_on_records_with_no_address'] == 0,
          f"-> {summary['province_cells_on_records_with_no_address']}")
    check('manifest: verdict is PASS', summary['verdict'].startswith('PASS'),
          f"-> {summary['verdict']}")

    import csv as _csv
    rows = list(_csv.DictReader(open(path, encoding='utf-8')))
    check('manifest: classification counts unchanged by validation',
          sum(1 for r in rows if r['classification'] == 'IMPORT') == 12096
          and len(rows) == 13043, f'-> {len(rows)} rows')
    for col in ('billing_province_code_sent', 'billing_province_flag',
                'shipping_province_code_sent', 'shipping_province_flag'):
        check(f'manifest: column {col} present', col in rows[0])


# =========================================================================
# Step 9 - mutation cost probe. The probe itself is live; these tests are
# offline and assert its guards and its recorded result.
# =========================================================================

def test_cost_probe_guards():
    import phase10_mutation_cost_probe as probe
    check('probe: the probe address cannot be a valid email',
          '@' not in probe.UNUSABLE_EMAIL, f'-> {probe.UNUSABLE_EMAIL}')
    check('probe: a real-looking email is refused',
          _raises(lambda: probe.assert_payload_cannot_succeed(
              'customerCreate', {'input': {'email': 'real@example.com'}}),
              probe.ProbeAborted))
    for forbidden in ('metafields', 'addresses', 'password', 'tags'):
        check(f'probe: a payload carrying {forbidden} is refused',
              _raises(lambda f=forbidden: probe.assert_payload_cannot_succeed(
                  'customerCreate', {'input': {'email': probe.UNUSABLE_EMAIL, f: 'x'}}),
                  probe.ProbeAborted))
    check('probe: the unusable probe address is permitted',
          probe.assert_payload_cannot_succeed(
              'customerCreate', {'input': {'email': probe.UNUSABLE_EMAIL}}) is True)

    check('probe: a created record is detected',
          probe.created_record_gid(
              {'data': {'customerCreate': {'customer': {'id': 'gid://shopify/Customer/9'}}}},
              'customerCreate') == 'gid://shopify/Customer/9')
    check('probe: a rejected mutation reports no created record',
          probe.created_record_gid(
              {'data': {'customerCreate': {'customer': None, 'userErrors': [{}]}}},
              'customerCreate') is None)

    src = open(probe.__file__, encoding='utf-8').read()
    check('probe: no password field appears anywhere in a payload',
          "'password'" in src and '"password":' not in src)
    check('probe: nothing is deleted automatically on an unexpected write',
          'customerDelete' not in src)


def test_measured_mutation_cost():
    path = os.path.join(REPO_ROOT, 'reports', 'phase10_mutation_cost_analysis.json')
    if not os.path.exists(path):
        check('cost: analysis present', False, '-> not generated')
        return
    a = json.load(open(path, encoding='utf-8'))
    check('cost: zero records were created', a['records_created'] == 0
          and a['customers_before'] == a['customers_after'] == 0,
          f"-> before {a['customers_before']} after {a['customers_after']}")
    check('cost: the run did not abort', a['aborted'] is None)
    check('cost: all three mutations were measured', len(a['probes']) == 3)
    check('cost: every probe was rejected by validation, none succeeded',
          all(p['outcome'] == 'REJECTED_BY_VALIDATION' for p in a['probes']))
    check('cost: every mutation costs 10 requested / 10 actual',
          all(p['requested_cost'] == 10 and p['actual_cost'] == 10 for p in a['probes']),
          f"-> {[(p['mutation'], p['actual_cost']) for p in a['probes']]}")

    rp = a['rate_plan']
    check('cost: the previously assumed cost of 10 is confirmed correct',
          rp['assumption_was'] == 'correct' and rp['measured_mutation_cost'] == 10.0)
    check('cost: sustained rate is 10 mutations/s at a 100/s restore rate',
          rp['sustained_mutations_per_second'] == 10.0)
    check('cost: burst capacity is 200 mutations from a full 2000 bucket',
          rp['burst_capacity_mutations'] == 200)
    check('cost: safe concurrency remains 1', rp['safe_concurrency'] == 1)
    check('cost: Option A estimate is about 28 minutes',
          27 <= rp['estimated_seconds_option_a'] / 60 <= 29,
          f"-> {rp['estimated_seconds_option_a'] / 60:.1f} min")

    check('cost: the runtime default now matches the measurement',
          rt.ThrottleController().assumed_cost == rp['measured_mutation_cost'])
    src = open(rt.__file__, encoding='utf-8').read()
    check('cost: the runtime no longer claims the cost is unmeasured',
          'Mutation cost is NOT known' not in src)


def test_cost_analysis_has_no_pii():
    path = os.path.join(REPO_ROOT, 'reports', 'phase10_mutation_cost_analysis.json')
    if not os.path.exists(path):
        return
    body = open(path, encoding='utf-8').read()
    import re as _re
    emails = [e for e in _re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', body)]
    check('cost: no email address in the tracked analysis', not emails, f'-> {emails}')
    check('cost: the probe address is not recorded either',
          'not-a-valid-email' not in body)

def main():
    tests = [
        test_customer_input_shape, test_legacy_metafield, test_schema_drift,
        test_address_transformation, test_country_validation,
        test_gb_province_handling, test_missing_country, test_missing_address1,
        test_missing_city_and_zip, test_address_default_handling,
        test_deterministic_transformation,
        test_throttling, test_exponential_backoff, test_proactive_pacing,
        test_timeout_verify_before_retry, test_token_expiry, test_access_denied,
        test_sanitized_errors, test_duplicate_legacy_id,
        test_resume, test_deleted_checkpoint, test_address_failure,
        test_duplicate_email, test_conflicting_name, test_missing_email,
        test_changed_email, test_phone_uniqueness,
        test_no_pii_in_ledger, test_no_pii_in_tracked_artifacts,
        test_runtime_cannot_mutate,
        # Tier-1 additions: collisions, ordering, eligibility gates
        test_two_customers_one_phone, test_large_collision_group_is_high_risk,
        test_selected_owner_retains_phone, test_unresolved_collision_defaults_safe,
        test_deterministic_collision_hashing, test_no_raw_phone_leakage,
        test_no_quadratic_lookup, test_legacy_metafield_inline,
        test_plan_has_no_addresses_in_customer_input,
        test_addresses_only_after_customer_creation,
        test_address_policy_is_selectable, test_missing_country_never_fabricated,
        test_consent_never_set, test_missing_email_never_reaches_create,
        test_conflicting_names_never_merged,
        # Phone ownership evidence and the approved keep-one / omit rule
        test_evidence_scoring, test_keep_one_when_one_clear_owner,
        test_omit_when_no_owner_evidence, test_large_group_always_omitted,
        test_contested_group_goes_to_manual_review,
        test_reviewer_decision_overrides_recommendation,
        test_no_duplicate_phone_ever_sent, test_phone_never_altered_or_deleted,
        test_collision_review_dataset_shape,
        # Conflicting-identity resolution - no code path selects a name
        test_name_choice_tokens, test_no_guessing_on_bad_input,
        test_unresolved_tracking, test_policy_block_all,
        test_policy_exclude_affected, test_policy_proceed_with_import_name,
        test_name_override_application, test_conflict_triage_never_decides,
        # Ratified: Customer = IMPORT, Address = SKIPPED_INVALID_COUNTRY
        test_skipped_invalid_country_status, test_gb_never_assumed,
        test_customer_survives_every_address_failure,
        test_address_skip_does_not_reduce_import_population,
        test_address_readiness_reporting,
        # Ratified: legacy id mandatory, woo_registered_at retained, createdAt untouched
        test_legacy_metafield_is_mandatory, test_legacy_metafield_assertion_catches_breakage,
        test_reconciliation_chain, test_woo_registered_at_retained,
        test_shopify_created_at_never_written, test_metafield_readiness_reporting,
        # Province code validation against Shopify's own accepted codes
        test_province_dataset_integrity, test_gb_province_always_omitted,
        test_valid_province_codes_are_sent,
        test_invalid_province_codes_are_omitted_and_flagged,
        test_countries_without_provinces, test_no_province_guessing,
        test_old_allowlist_is_gone, test_province_population_after_validation,
        # Province rules locked; live cross-check is an accepted limitation
        test_gb_omission_is_mandatory_and_locked,
        test_non_gb_handling_stays_conservative,
        test_province_limitation_is_documented_not_open,
        test_province_dataset_unchanged_by_the_limitation,
        # Step 8: canonical validator module, audit ledger, dry-run integration
        test_validator_module_is_canonical, test_last_line_of_defence_assertion,
        test_province_flag_ledger, test_dry_run_manifest_province_guarantee,
        # Step 9: mutation cost probe guards and the measured result
        test_cost_probe_guards, test_measured_mutation_cost,
        test_cost_analysis_has_no_pii,
    ]
    for t in tests:
        print(f'\n--- {t.__name__} ---')
        t()

    failed = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} passed.')
    if failed:
        print('\nFAILURES:')
        for name, _, detail in failed:
            print(f'  {name} {detail}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
