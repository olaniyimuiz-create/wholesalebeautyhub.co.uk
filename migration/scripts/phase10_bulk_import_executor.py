"""Phase 10 bulk customer import executor. GATE 7 IS NOT AUTHORIZED.

This is the program that would migrate the approved 11,849-customer population.
It is the largest write capability in the project by three orders of magnitude,
so its design is mostly about what it refuses to do.

    MANIFEST -> PRE-FLIGHT -> LIVE LEGACY-ID MAP -> PLAN -> WRITE -> LEDGER

RELATIONSHIP TO THE OTHER TWO EXECUTORS
---------------------------------------
    phase10_bulk_import.py        dry-run ONLY, no mutation document at all.
                                  UNCHANGED by this file and still incapable of
                                  writing. It remains the safe way to re-plan.
    phase10_tier3_executor.py     three named tests, at most 10 records.
    this file                     the bulk run, and nothing smaller.

Three separate programs rather than one with a mode flag, because a flag is a
thing that gets passed by accident and a program is a thing you have to choose.

NOTHING IS REIMPLEMENTED
------------------------
Transformation, throttling, backoff, response classification, PII sanitisation,
ledger and checkpoint semantics, the legacy-ID map and the risk #45 phone
fallback all come from phase10_import_runtime - offline, mutation-refusing, and
covered by 644 assertions. The runtime decides; only the send is local, because
the runtime refuses mutation documents by construction and that guarantee is not
being weakened for the biggest write in the project.

DEFAULT IS PLAN-ONLY. Live transport requires the exact Gate 7 phrase, and the
phrase is read from the frozen contract rather than hardcoded here, so there is
one authoritative copy rather than three that can drift.

Run: python migration/scripts/phase10_bulk_import_executor.py            # plan
     python migration/scripts/phase10_bulk_import_executor.py --execute \\
            --authorization "<exact Gate 7 phrase>" --expect-commit <sha>
"""
import csv
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt

# --------------------------------------------------------------------------
# Frozen constants - every one of these is compared against by a guard
# --------------------------------------------------------------------------

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'schema')
CONTRACT_PATH = os.path.join(SCHEMA_DIR, 'phase10_migration_contract.json')
CONTRACT_SHA256 = 'd889fd03a0122ece7dcc1c741381a87cba90506eabe7d074608a6459ebc9feb0'

MANIFEST_PATH = os.path.join('reports', 'phase10_customer_manifest.csv')
MANIFEST_SHA256 = '2e31f3edbed607d3cec3bf2790ee9deae57e7c0e2f7dd07c14a0b1c4bcecdfda'

APPROVED_STORE_DOMAIN = 'wholesale-beautyhub.myshopify.com'
EXPECTED_API_VERSION = '2026-07'
REQUIRED_SCOPES = frozenset({'read_customers', 'write_customers'})

APPROVED_IMPORT_POPULATION = 12096      # manifest IMPORT rows
APPROVED_RUN_POPULATION = 11849         # after ADR-014 Gate 5

LEDGER_PATH = os.path.join('reports', 'phase10_bulk_import_ledger.jsonl')
CHECKPOINT_PATH = os.path.join('reports', 'phase10_bulk_import_checkpoint.jsonl')
PLAN_PATH = os.path.join('reports', 'phase10_bulk_import_executor_plan.json')
RESULT_PATH = os.path.join('reports', 'phase10_bulk_import_executor_result.json')

MODE_PLAN = 'plan'
MODE_EXECUTE = 'execute'
DEFAULT_MODE = MODE_PLAN                # GUARD 3/4

# Measured against this store, not assumed: 10 points per mutation, bucket
# restoring 100/s, so 10 mutations/s spends exactly what is restored.
TARGET_MUTATIONS_PER_SECOND = 10


class Halt(RuntimeError):
    """Stop. Nothing further is sent, and what was written stays written."""


class NotAuthorized(Halt):
    """The exact Gate 7 phrase was not supplied."""


# --------------------------------------------------------------------------
# Mutation documents - the minimum, and nothing else
# --------------------------------------------------------------------------

CUSTOMER_CREATE = '''mutation bulkCustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id
               metafield(namespace: "custom", key: "legacy_woo_customer_id") { value } }
    userErrors { field message }
  }
}'''

CUSTOMER_ADDRESS_CREATE = '''mutation bulkAddressCreate($customerId: ID!, $address: MailingAddressInput!, $setAsDefault: Boolean) {
  customerAddressCreate(customerId: $customerId, address: $address, setAsDefault: $setAsDefault) {
    address { id }
    userErrors { field message }
  }
}'''

PREFLIGHT_QUERY = '''{
  shop { name myshopifyDomain plan { displayName partnerDevelopment } }
  currentAppInstallation { accessScopes { handle } }
  customersCount { count }
  __type(name: "CustomerInput") { inputFields { name } }
}'''

# There is deliberately NO customerDelete document in this file. A bulk deleter
# is not a thing this project needs to own, and rollback is per-record and
# separately authorized. See PHASE10_IMPORT_PROCEDURE.md section 11.


# --------------------------------------------------------------------------
# GUARDS 1-16
# --------------------------------------------------------------------------

def load_contract():
    with open(CONTRACT_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def gate_7_phrase():
    """The authorization phrase, from the frozen contract.

    Read rather than hardcoded so there is exactly one authoritative copy. A
    second literal in this file could drift from the contract, and the drift
    would be discovered by an operator whose correct phrase was rejected - or,
    far worse, by one whose wrong phrase was accepted.
    """
    return load_contract()['authorization']['gate_7_phrase']


def guard_1_approved_store(domain):
    if (domain or '').strip().lower() != APPROVED_STORE_DOMAIN:
        raise Halt(f'GUARD 1: store {domain!r} is not the approved store '
                   f'{APPROVED_STORE_DOMAIN!r}. Refusing.')
    return True


def guard_2_reject_production(shop):
    """Two independent signals, and missing information counts as production."""
    plan = (shop or {}).get('plan') or {}
    if plan.get('partnerDevelopment') is not True:
        raise Halt('GUARD 2: target is not a development store '
                   f'(plan={plan.get("displayName")!r}, '
                   f'partnerDevelopment={plan.get("partnerDevelopment")!r}). Refusing.')
    domain = (shop.get('myshopifyDomain') or '').lower()
    if any(marker in domain for marker in ('-prod', 'production', 'live-')):
        raise Halt(f'GUARD 2: domain {domain!r} carries a production marker. Refusing.')
    return True


def guard_3_live_is_not_default(mode_from_argv):
    if mode_from_argv is None:
        return MODE_PLAN
    if mode_from_argv not in (MODE_PLAN, MODE_EXECUTE):
        raise Halt(f'GUARD 3: unknown mode {mode_from_argv!r}')
    return mode_from_argv


def guard_4_plan_is_the_default():
    if DEFAULT_MODE != MODE_PLAN:
        raise Halt('GUARD 4: the default mode is not plan-only. Refusing to start.')
    return True


def guard_5_gate_7_authorization(supplied):
    """The exact phrase from the contract, and nothing else. Not a paraphrase,
    not a previous approval, not a Tier-3 authorization, not a recommendation."""
    expected = gate_7_phrase()
    if (supplied or '') != expected:
        raise NotAuthorized(
            'GUARD 5: no valid Gate 7 execution authorization supplied. The exact '
            'approval phrase recorded in the frozen migration contract is required; '
            'nothing else counts, including a Tier-3 authorization or a previous '
            'approval of any kind.')
    return True


def guard_6_manifest_hash(path=MANIFEST_PATH, expected=MANIFEST_SHA256):
    if not os.path.exists(path):
        raise Halt(f'GUARD 6: approved manifest {path} not found.')
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    if digest != expected:
        raise Halt(f'GUARD 6: manifest hash mismatch. Approved {expected[:16]}..., '
                   f'found {digest[:16]}.... Re-approval is required, not a re-run.')
    return digest


def guard_6b_contract_hash(path=CONTRACT_PATH, expected=CONTRACT_SHA256):
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    if digest != expected:
        raise Halt(f'GUARD 6b: frozen contract hash mismatch: {digest[:16]}....')
    return digest


def guard_7_population(manifest_ids, run_ids):
    """Counts are not enough. Two different populations of the same size pass a
    count check and import the wrong people, so the ids are compared as sets."""
    if len(manifest_ids) != APPROVED_IMPORT_POPULATION:
        raise Halt(f'GUARD 7: manifest holds {len(manifest_ids)} IMPORT rows, '
                   f'approved {APPROVED_IMPORT_POPULATION}.')
    if len(set(manifest_ids)) != len(manifest_ids):
        raise Halt('GUARD 7: the manifest contains duplicate Woo ids.')
    if len(run_ids) != APPROVED_RUN_POPULATION:
        raise Halt(f'GUARD 7: run population is {len(run_ids)}, approved '
                   f'{APPROVED_RUN_POPULATION}.')
    outside = set(run_ids) - set(manifest_ids)
    if outside:
        raise Halt(f'GUARD 7: {len(outside)} run id(s) are not approved manifest rows.')
    return True


def guard_8_store_state(count, legacy_map):
    """The store must be in a state this run can reason about.

    An empty store is the clean case. A store already holding records is the
    RESUME case, and it is only safe when every customer present carries a
    legacy id this run recognises - otherwise something else has been writing
    and the idempotency map cannot be trusted.
    """
    known = len(legacy_map)
    if count != known:
        raise Halt(f'GUARD 8: the store holds {count} customer(s) but only {known} '
                   f'carry a recognised legacy id. Something not from this migration '
                   f'has written to the store; halting rather than guessing.')
    return True


def guard_9_no_duplicate_legacy_ids(woo_ids):
    seen, duplicates = set(), set()
    for woo_id in woo_ids:
        if woo_id in seen:
            duplicates.add(woo_id)
        seen.add(woo_id)
    if duplicates:
        raise Halt(f'GUARD 9: {len(duplicates)} duplicate legacy Woo id(s) in the '
                   f'cohort. The resume key is not unique. Halting.')
    return True


def guard_10_skip_if_present(woo_id, legacy_map):
    """Already live means skip, never create twice."""
    existing = legacy_map.get(str(woo_id))
    return existing.get('gid') if existing else None


def guard_11_verify_before_retry():
    import inspect
    if 'verify' not in inspect.signature(rt.execute_with_retry).parameters:
        raise Halt('GUARD 11: the runtime no longer supports verify-before-retry.')
    return True


def guard_12_auth_failure_halts():
    klass, _ = rt.classify_response(
        {'errors': [{'message': 'x', 'extensions': {'code': 'ACCESS_DENIED'}}]})
    if klass != rt.AUTH_FAILURE:
        raise Halt('GUARD 12: the runtime no longer classifies ACCESS_DENIED as an '
                   'auth failure.')
    return True


def guard_13_throttle_backoff():
    klass, _ = rt.classify_response(
        {'errors': [{'message': 'x', 'extensions': {'code': 'THROTTLED'}}]})
    if klass != rt.THROTTLED or rt.BACKOFF_SCHEDULE != (1, 2, 4, 8, 16):
        raise Halt('GUARD 13: the runtime no longer implements the approved backoff.')
    return True


def guard_14_legacy_metafield_inline(customer_input, woo_id):
    """No customer is created without its legacy id in the SAME call. This is
    the identity chain the whole migration hangs on: a customer without it is
    unmatchable, unskippable on resume, and unfindable for rollback."""
    try:
        rt.assert_legacy_metafield_present(customer_input)
    except rt.LegacyMetafieldMissing as exc:
        raise Halt(f'GUARD 14: woo_customer_id={woo_id} payload lacks the legacy '
                   f'metafield: {exc}') from None
    return True


def guard_15_within_cohort(woo_id, approved_ids):
    if int(woo_id) not in approved_ids:
        raise Halt(f'GUARD 15: woo_customer_id={woo_id} is not in the approved run '
                   f'population. Refusing.')
    return True


def guard_16_verify_supplied_cohort(path, approved_ids):
    """A runtime-supplied id list is only accepted if every id is approved, and
    its hash is recorded. An operator-supplied list is the most direct route to
    importing the wrong people."""
    if not os.path.exists(path):
        raise Halt(f'GUARD 16: cohort file {path} not found.')
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    ids = [int(line.strip()) for line in open(path, encoding='utf-8')
           if line.strip().isdigit()]
    if not ids:
        raise Halt(f'GUARD 16: cohort file {path} contains no Woo customer ids.')
    outside = sorted(set(ids) - approved_ids)
    if outside:
        raise Halt(f'GUARD 16: {len(outside)} id(s) in {path} are not in the approved '
                   f'run population. Refusing the whole file.')
    return ids, digest


GUARDS = [
    (1, 'approved store identified'), (2, 'production store rejected'),
    (3, 'live is not the default'), (4, 'plan-only is the default'),
    (5, 'missing Gate 7 authorization halts'), (6, 'manifest hash matches'),
    (7, 'population matches the approved manifest, by id set'),
    (8, 'store state is empty or a recognised resume'),
    (9, 'duplicate legacy Woo ids halt'), (10, 'already-present legacy id skips'),
    (11, 'timeout verifies before retrying'),
    (12, '401 / token expiry halts immediately'),
    (13, 'THROTTLED uses the tested backoff'),
    (14, 'legacy id written in the same customerCreate'),
    (15, 'no creation outside the approved population'),
    (16, 'supplied cohort verified against the manifest'),
]


# --------------------------------------------------------------------------
# Population
# --------------------------------------------------------------------------

def load_manifest_ids(path=MANIFEST_PATH):
    """The approved IMPORT ids, in manifest order."""
    ids = []
    with open(path, encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('classification') == 'IMPORT':
                ids.append(int(row['woo_customer_id']))
    return ids


_SOURCE_CACHE = {}


def source_population():
    """Candidates and the Gate 5 deferral set, parsed once per process.

    Field values come from the source, not the manifest: the manifest carries no
    shipping postcode or province, and a shipping-fallback customer built from
    it silently loses its postcode. Measured on woo 957 during Tier-3.
    """
    if 'by_id' not in _SOURCE_CACHE:
        from phase10_run_plan import load_population  # noqa: PLC0415
        imports, conflicted, consent = load_population()
        _SOURCE_CACHE.update({'by_id': {c['woo_customer_id']: c for c in imports},
                              'conflicted': set(conflicted), 'consent': consent})
    return _SOURCE_CACHE['by_id'], _SOURCE_CACHE['conflicted']


def phone_decisions(candidates):
    """{woo_id: SEND_PHONE|OMIT_PHONE|HOLD} under ADR-014 Gate 1, which signed
    the evidence-scored recommendations with no reviewer override."""
    by_key = {}
    for cand in candidates:
        if (cand.get('phone') or '').strip():
            by_key.setdefault(rt.phone_canonical(cand.get('phone')), []).append(cand)
    actions = {}
    for members in (v for v in by_key.values() if len(v) > 1):
        action, owner, _why = rt.recommend_group_action(members)
        for member in members:
            actions[member['woo_customer_id']] = rt.final_phone_action(
                member['woo_customer_id'], action, owner)
    return actions


def build_run_population():
    """The approved 11,849, with their plans. Offline; no network."""
    guard_6_manifest_hash()
    guard_6b_contract_hash()
    manifest_ids = load_manifest_ids()
    by_id, conflicted = source_population()
    run_ids = [w for w in manifest_ids if w not in conflicted]
    guard_7_population(manifest_ids, run_ids)
    guard_9_no_duplicate_legacy_ids(run_ids)

    actions = phone_decisions(list(by_id.values()))
    policy = load_contract()['address']['policy']
    if policy != rt.ADDRESS_POLICY_RATIFIED:
        raise Halt(f'contract address policy {policy!r} does not match the runtime.')

    plans = {}
    for woo_id in run_ids:
        cand = by_id[woo_id]
        has_phone = bool((cand.get('phone') or '').strip())
        send = has_phone and actions.get(woo_id, rt.SEND_PHONE) == rt.SEND_PHONE
        plans[woo_id] = rt.plan_customer_import(cand, phone_allowed=send,
                                                address_policy=policy)
    return run_ids, plans


def assert_payload_contract(payload, woo_id):
    """Exactly the fields the contract permits, and no others."""
    forbidden = {'addresses', 'emailMarketingConsent', 'smsMarketingConsent',
                 'whatsAppMarketingConsent', 'password', 'username',
                 'wp_capabilities', 'capabilities', 'company'}
    present = forbidden & set(payload)
    if present:
        raise Halt(f'woo_customer_id={woo_id}: payload carries forbidden field(s) '
                   f'{sorted(present)}. Addresses are a separate call, consent is a '
                   f'separate pass, company belongs on the address.')
    unknown = set(payload) - {'email', 'firstName', 'lastName', 'phone', 'tags',
                              'metafields'}
    if unknown:
        raise Halt(f'woo_customer_id={woo_id}: undocumented field(s) {sorted(unknown)}.')
    guard_14_legacy_metafield_inline(payload, woo_id)
    rt.assert_no_server_controlled_fields(payload)
    return True


# --------------------------------------------------------------------------
# Sending - the runtime's policy, a local loop
# --------------------------------------------------------------------------

def send_mutation(send, document, variables, throttle=None, verify=None,
                  sleep=None, max_transient=None, max_throttle_retries=50):
    """One mutation, paced and retried under the runtime's tested policy.

    The loop is local because rt.execute_with_retry refuses mutation documents
    by construction. Every DECISION is the runtime's: pacing from
    extensions.cost, response and exception classification, backoff timing and
    halt conditions. No schedule, jitter or classification is defined here.
    """
    sleep = sleep or time.sleep
    max_transient = rt.MAX_TRANSIENT_ATTEMPTS if max_transient is None else max_transient
    transient = throttled = attempts = 0
    while True:
        if throttle is not None:
            throttle.pace()
        attempts += 1
        try:
            response = send(document, variables)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            klass, detail = rt.classify_exception(exc)
            if klass == rt.AUTH_FAILURE:
                raise rt.HaltMigration(
                    f'authentication failed, halting: {detail}') from None
            if klass == rt.THROTTLED:
                throttled += 1
                if throttled > max_throttle_retries:
                    raise rt.HaltMigration('throttled past the retry ceiling') from None
                sleep(rt.backoff_delay(throttled))
                continue
            # Ambiguous. The write may have committed before the socket died,
            # so ask before re-sending rather than risk a duplicate customer.
            if verify is not None and verify():
                return ({'data': None, '_verified_existing': True}, attempts)
            transient += 1
            if transient >= max_transient:
                raise
            sleep(rt.backoff_delay(transient))
            continue
        if throttle is not None:
            throttle.observe(response)
        klass, detail = rt.classify_response(response)
        if klass == rt.THROTTLED:
            throttled += 1
            if throttled > max_throttle_retries:
                raise rt.HaltMigration('throttled past the retry ceiling')
            sleep(rt.backoff_delay(throttled))
            continue
        if klass == rt.AUTH_FAILURE:
            raise rt.HaltMigration(f'authentication failed, halting: {detail}')
        return response, attempts


# --------------------------------------------------------------------------
# Plan mode - offline, and the default
# --------------------------------------------------------------------------

def plan(write=True):
    """Everything the run would do, computed offline. Sends nothing."""
    guard_4_plan_is_the_default()
    guard_11_verify_before_retry()
    guard_12_auth_failure_halts()
    guard_13_throttle_backoff()

    run_ids, plans = build_run_population()
    approved = set(run_ids)
    creates = addresses = with_phone = fallback_expected = 0
    two_metafields = 0
    for woo_id in run_ids:
        guard_15_within_cohort(woo_id, approved)
        stages = plans[woo_id]
        payload = stages[0]['input']
        assert_payload_contract(payload, woo_id)
        creates += 1
        addresses += sum(1 for s in stages if s['stage'] == rt.STAGE_ADDRESS)
        if len(payload['metafields']) == 2:
            two_metafields += 1
        if 'phone' in payload:
            with_phone += 1
            category, _reason = rt.classify_phone_format(payload['phone'])
            if (category == rt.PHONE_FORMAT_INVALID
                    or rt.phone_plan_advisory(payload['phone'])):
                fallback_expected += 1

    total = creates + addresses
    worst = total + fallback_expected
    report = {
        'mode': MODE_PLAN,
        'shopify_mutations_performed': 0,
        'executor_commit': reviewed_commit(),
        'head_commit': git_head(),
        'manifest_sha256': MANIFEST_SHA256,
        'contract_sha256': CONTRACT_SHA256,
        'population': {'approved_import': APPROVED_IMPORT_POPULATION,
                       'run_population': len(run_ids)},
        'mutations': {'customerCreate': creates, 'customerAddressCreate': addresses,
                      'total': total,
                      'phone_fallback_second_creates_expected': fallback_expected,
                      'total_worst_case': worst},
        'content': {'customers_with_a_phone_sent': with_phone,
                    'customers_with_both_metafields': two_metafields,
                    'consent_applied_by_this_run': 0},
        'rate_plan': {'target_mutations_per_second': TARGET_MUTATIONS_PER_SECOND,
                      'estimated_minutes': round(worst / TARGET_MUTATIONS_PER_SECOND / 60, 1),
                      'expected_throttles': 0},
        'guards': [{'guard': n, 'rule': r} for n, r in GUARDS],
        'authorization': 'ADR-014 Gate 7 is UNSIGNED. This is a plan, not permission.',
    }
    if write:
        os.makedirs(os.path.dirname(PLAN_PATH), exist_ok=True)
        with open(PLAN_PATH, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    return report


# --------------------------------------------------------------------------
# Commit pinning
# --------------------------------------------------------------------------

BEHAVIOUR_PATHS = (
    'migration/scripts/phase10_bulk_import_executor.py',
    'migration/scripts/phase10_import_runtime.py',
    'migration/scripts/phase10_province_validator.py',
    'migration/schema/phase10_migration_contract.json',
)


def git_head():
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ''


def git_tree_is_clean():
    try:
        return subprocess.run(['git', 'status', '--porcelain'], capture_output=True,
                              text=True, timeout=30).stdout.strip() == ''
    except Exception:  # noqa: BLE001
        return False


def reviewed_commit(paths=BEHAVIOUR_PATHS):
    """The most recent commit touching a file that decides behaviour. Not HEAD:
    a document commit must not invalidate an approval given against the code."""
    latest = None
    for path in paths:
        try:
            out = subprocess.run(['git', 'log', '-1', '--format=%H %ct', '--', path],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            continue
        if not out:
            continue
        sha, _sep, when = out.partition(' ')
        stamp = int(when or 0)
        if latest is None or stamp > latest[1]:
            latest = (sha, stamp)
    return latest[0] if latest else ''


def assert_expected_commit(expected, tree_check=None):
    if not expected:
        raise Halt('live execution requires --expect-commit naming the reviewed '
                   'executor commit.')
    reviewed = reviewed_commit()
    if not (reviewed.startswith(expected) or expected.startswith(reviewed[:len(expected)])):
        raise Halt(f'commit mismatch: approval names {expected[:12]}, the reviewed '
                   f'executor commit is {reviewed[:12]}.')
    if not (tree_check or git_tree_is_clean)():
        raise Halt('the working tree is dirty. The code about to run is not the '
                   'code that was reviewed.')
    return reviewed


# --------------------------------------------------------------------------
# Live pre-flight
# --------------------------------------------------------------------------

def preflight(send, domain, api_version):
    guard_1_approved_store(domain)
    if (api_version or '') != EXPECTED_API_VERSION:
        raise Halt(f'GUARD: API version {api_version!r} is not the pinned '
                   f'{EXPECTED_API_VERSION!r}.')
    response = send(PREFLIGHT_QUERY, None)
    klass, detail = rt.classify_response(response)
    if klass == rt.AUTH_FAILURE:
        raise Halt(f'GUARD: authentication failed - {detail}')
    if klass != rt.OK:
        raise Halt(f'GUARD: pre-flight failed - {klass}: {detail}')
    data = response['data']
    guard_2_reject_production(data['shop'])
    scopes = {s['handle'] for s in data['currentAppInstallation']['accessScopes']}
    missing = REQUIRED_SCOPES - scopes
    if missing:
        raise Halt(f'GUARD: missing scope(s) {sorted(missing)}.')
    fields = {f['name'] for f in (data.get('__type') or {}).get('inputFields', [])}
    if 'addresses' in fields:
        raise Halt('GUARD: schema drift - CustomerInput now exposes `addresses`.')
    for required in ('email', 'metafields', 'phone', 'tags'):
        if required not in fields:
            raise Halt(f'GUARD: schema drift - CustomerInput has no {required!r}.')
    return {'store': data['shop']['myshopifyDomain'], 'scopes': len(scopes),
            'customers_before': data['customersCount']['count'],
            'api_version': api_version, 'development_store': True}


# --------------------------------------------------------------------------
# Live execution
# --------------------------------------------------------------------------

def execute(authorization, expect_commit, send, domain, api_version,
            throttle=None, sleep=None, tree_check=None, limit=None):
    """The bulk run. Every guard above must pass before a single mutation.

    `send` is injected: the caller owns transport, and the offline suite passes
    a mock so no test can reach Shopify.
    """
    guard_5_gate_7_authorization(authorization)
    assert_expected_commit(expect_commit, tree_check=tree_check)
    guard_6_manifest_hash()
    guard_6b_contract_hash()

    state = preflight(send, domain, api_version)
    run_ids, plans = build_run_population()
    approved = set(run_ids)

    legacy_map, _pages = rt.fetch_existing_legacy_map(send)
    guard_8_store_state(state['customers_before'], legacy_map)

    run_id = 'bulk-' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    ledger = rt.ImportLedger(ledger_path=LEDGER_PATH, checkpoint_path=CHECKPOINT_PATH,
                             run_id=run_id, importer_commit=reviewed_commit())
    throttle = throttle if throttle is not None else rt.ThrottleController()

    created, skipped, failed, fallbacks = [], [], [], []
    mutations = {'customerCreate': 0, 'customerAddressCreate': 0}
    targets = run_ids if limit is None else run_ids[:limit]

    for woo_id in targets:
        guard_15_within_cohort(woo_id, approved)
        existing = guard_10_skip_if_present(woo_id, legacy_map)
        if existing:
            skipped.append(woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'SKIPPED_ALREADY_PRESENT',
                          shopify_gid=existing, reconciliation_status='RESUMED')
            continue

        stages = plans[woo_id]
        payload = stages[0]['input']
        assert_payload_contract(payload, woo_id)

        def verify(_woo=woo_id):
            return bool(rt.fetch_existing_legacy_map(send)[0].get(str(_woo)))

        response, attempts = send_mutation(send, CUSTOMER_CREATE, {'input': payload},
                                           throttle=throttle, verify=verify, sleep=sleep)
        if response.get('_verified_existing'):
            raise Halt(f'woo {woo_id}: an ambiguous failure was verified as having '
                       f'landed. Halting for reconciliation rather than retrying.')
        mutations['customerCreate'] += 1
        result = response['data']['customerCreate']
        errors = result.get('userErrors') or []

        # Risk #45: a rejected phone costs the phone, never the customer.
        if (errors or not result.get('customer')) and rt.is_phone_user_error(errors) \
                and 'phone' in payload:
            fallback = rt.phone_fallback(payload, errors, woo_id,
                                         operation=rt.STAGE_CUSTOMER, run_id=run_id,
                                         log_path=rt.DROPPED_PHONES_PATH)
            retry_payload = fallback['input']
            assert_payload_contract(retry_payload, woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'PHONE_DROPPED_RETRYING',
                          attempt=attempts, error_class=fallback['event']['reason'],
                          reconciliation_status='PENDING')
            response, retry_attempts = send_mutation(
                send, CUSTOMER_CREATE, {'input': retry_payload},
                throttle=throttle, sleep=sleep)
            mutations['customerCreate'] += 1
            attempts += retry_attempts
            fallbacks.append(woo_id)
            result = response['data']['customerCreate']
            errors = result.get('userErrors') or []

        if errors or not result.get('customer'):
            failed.append(woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'FAILED', attempt=attempts,
                          error_class='USER_ERROR',
                          error_detail=json.dumps(rt.sanitize_user_errors(errors)),
                          reconciliation_status='QUARANTINED')
            continue

        gid = result['customer']['id']
        live_legacy = (result['customer'].get('metafield') or {}).get('value')
        if live_legacy != str(woo_id):
            raise Halt(f'woo {woo_id}: created {gid} carries legacy id '
                       f'{live_legacy!r}. The identity chain is broken; halting.')
        created.append(woo_id)
        ledger.record(woo_id, rt.STAGE_CUSTOMER,
                      'CREATED_PHONE_DROPPED' if woo_id in fallbacks else 'CREATED',
                      shopify_gid=gid, attempt=attempts,
                      reconciliation_status='PENDING')

        for stage in stages[1:]:
            aresp, aattempts = send_mutation(
                send, CUSTOMER_ADDRESS_CREATE,
                {'customerId': gid, 'address': stage['address'],
                 'setAsDefault': stage['setAsDefault']},
                throttle=throttle, sleep=sleep)
            mutations['customerAddressCreate'] += 1
            ares = aresp['data']['customerAddressCreate']
            aerrors = ares.get('userErrors') or []
            if aerrors or not ares.get('address'):
                # The customer survives an address failure and the address is
                # retried on its own - never by recreating the customer.
                ledger.record(woo_id, rt.STAGE_ADDRESS, 'ADDRESS_FAILED',
                              shopify_gid=gid, attempt=aattempts,
                              error_class='USER_ERROR',
                              error_detail=json.dumps(rt.sanitize_user_errors(aerrors)),
                              address_status='RETRYABLE',
                              reconciliation_status='PENDING')
            else:
                ledger.record(woo_id, rt.STAGE_ADDRESS, 'ADDRESS_CREATED',
                              shopify_gid=gid, attempt=aattempts,
                              address_status=rt.ADDRESS_STATUS_PLANNED,
                              reconciliation_status='PENDING')

    if len(created) > APPROVED_RUN_POPULATION:
        raise Halt(f'{len(created)} customers created exceeded the approved '
                   f'{APPROVED_RUN_POPULATION}.')

    result = {
        'mode': MODE_EXECUTE, 'run_id': run_id,
        'executor_commit': reviewed_commit(), 'head_commit': git_head(),
        'manifest_sha256': MANIFEST_SHA256, 'contract_sha256': CONTRACT_SHA256,
        'store_state_before': state,
        'created': len(created), 'skipped_already_present': len(skipped),
        'failed': len(failed), 'phone_fallbacks': len(fallbacks),
        'mutations': mutations, 'total_mutations': sum(mutations.values()),
        'consent_applied': 0,
    }
    ledger.write_result(path=RESULT_PATH, extra=result)
    return result


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_argv(argv):
    mode = authorization = expect_commit = cohort_file = None
    for index, arg in enumerate(argv):
        if arg == '--execute':
            mode = MODE_EXECUTE
        elif arg == '--plan':
            mode = MODE_PLAN
        elif arg == '--authorization' and index + 1 < len(argv):
            authorization = argv[index + 1]
        elif arg == '--expect-commit' and index + 1 < len(argv):
            expect_commit = argv[index + 1]
        elif arg == '--cohort-file' and index + 1 < len(argv):
            cohort_file = argv[index + 1]
    return mode, authorization, expect_commit, cohort_file


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    requested, authorization, expect_commit, cohort_file = parse_argv(argv)
    mode = guard_3_live_is_not_default(requested)

    print('Phase 10 bulk customer import executor.')
    print('ADR-014 Gate 7 is UNSIGNED. Plan mode is the default and writes nothing.\n')

    if mode == MODE_PLAN:
        report = plan()
        print(json.dumps(report, indent=2, sort_keys=True))
        print(f'\nWrote {PLAN_PATH}')
        print('\nSHOPIFY MUTATIONS: 0')
        return 0

    # Live path. Authorization is checked before transport is even imported.
    guard_5_gate_7_authorization(authorization)

    from phase9_preflight import get_config, graphql_request  # noqa: PLC0415
    config = get_config()
    domain, token = config['domain'], config['token']
    if not domain or not token:
        print('NOT_CONFIGURED - nothing sent.')
        return 2

    def send(document, variables=None):
        return graphql_request(domain, token, config['api_version'], document, variables)

    if cohort_file:
        _run_ids, _plans = build_run_population()
        ids, digest = guard_16_verify_supplied_cohort(cohort_file, set(_run_ids))
        print(f'GUARD 16: {len(ids)} supplied id(s) verified, sha256={digest[:16]}...')

    result = execute(authorization, expect_commit, send, domain, config['api_version'])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Halt as halt:
        print(f'\n*** HALTED: {halt}')
        sys.exit(1)
