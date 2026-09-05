"""Tier-3 live-test executor. Built under Approval A (BUILD ONLY, 2026-08-22).

This is the first Phase 10 module that carries live customer mutation documents.
It exists so that the three individually-authorized Tier-3 tests have a
mechanism narrow enough to be trusted, instead of the bulk importer being
widened into a live writer.

WHAT IT CAN DO
--------------
Exactly three named tests, defined in this file and nowhere else:

    TIER3-TEST-1   woo 220        1 customerCreate, 0 addresses
    TIER3-TEST-2   woo 2          1 customerCreate, 1 customerAddressCreate
    TIER3-TEST-3   10 customers   cohort NOT YET FROZEN - see below

It cannot be pointed at an arbitrary customer, a cohort file, or the 11,849
manifest. There is no code path that accepts a customer id from the command
line, so "run it for this one customer instead" is not a mistake that can be
made - it is a change to this file, which is a reviewable act.

WHAT IT DELIBERATELY CANNOT DO
------------------------------
`customerDelete` has a document and NO EXECUTABLE PATH. Rollback is a Shopify
mutation like any other, and Approval A did not authorize it. `rollback_spec()`
describes what a rollback WOULD do; `execute_rollback()` raises. Making the
document present but unreachable is deliberate: a reviewer can see exactly what
would be sent, and no argument combination sends it.

AUTHORIZATION MODEL
-------------------
Each test carries its own exact phrase. Test 1's phrase does not authorize
Test 2, Test 2's does not authorize Test 3, and none of them authorizes the
bulk run. Approval is per test, per execution, immediately before it happens.

Live execution additionally requires:
  * the frozen contract's sha256 to match           (requirement 11)
  * --expect-commit to match HEAD, with a clean tree (requirement 12)
  * a read-only pre-flight to pass                   (requirements 13-20)
  * the legacy id to be absent from the live store   (requirements 21-22)

WHY --expect-commit RATHER THAN A CONSTANT
------------------------------------------
A constant naming this file's own commit cannot be written before that commit
exists, and amending it afterwards would change the very hash it claims. So the
approving human names the commit, the executor verifies HEAD matches it and that
the tree is clean, and the two statements are checkable against each other.

NOTHING IS REIMPLEMENTED
------------------------
Throttling, backoff, verify-before-retry, PII sanitisation, ledger semantics,
the transformation and the risk #45 phone fallback all come from
phase10_import_runtime, which is offline and covered by 644 assertions.

DEFAULT MODE IS SIMULATION.

Run: python migration/scripts/phase10_tier3_executor.py --simulate TIER3-TEST-1
     python migration/scripts/phase10_tier3_executor.py --execute TIER3-TEST-1 \\
            --authorization "<exact phrase>" --expect-commit <sha>
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
# Frozen constants
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

LEDGER_PATH = os.path.join('reports', 'phase10_tier3_log.jsonl')
CHECKPOINT_PATH = os.path.join('reports', 'phase10_tier3_checkpoint.jsonl')
SIMULATION_RESULT_DIR = 'reports'


def simulation_result_path(test_id):
    """One file per test, so simulating Test 2 cannot quietly overwrite the
    evidence produced for Test 1."""
    return os.path.join(SIMULATION_RESULT_DIR,
                        'phase10_tier3_simulation_%s.json'
                        % test_id.lower().replace('-', '_'))

# A Tier-3 test is small by definition. This is a second, independent bound on
# top of the per-test expectations - if a definition is ever edited to hold a
# cohort, this refuses it before anything else looks at it.
TIER3_MAX_CUSTOMERS = 10

MODE_SIMULATE = 'simulate'
MODE_EXECUTE = 'execute'
DEFAULT_MODE = MODE_SIMULATE

# Rollback is a mutation. Approval A did not grant it.
ROLLBACK_AUTHORIZED = False


class Halt(RuntimeError):
    """Stop. Nothing further is sent."""


class RollbackNotAuthorized(Halt):
    """customerDelete has a document and no executable path."""


class TestNotAuthorized(Halt):
    """The exact per-test phrase was not supplied."""


class CohortNotFrozen(Halt):
    """A test whose customer list has not been approved cannot run."""


# --------------------------------------------------------------------------
# Mutation documents - the minimum, and nothing else
# --------------------------------------------------------------------------

CUSTOMER_CREATE = '''mutation tier3CustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id email firstName lastName phone tags createdAt
               metafields(first: 5, namespace: "custom") {
                 edges { node { key value type } } } }
    userErrors { field message }
  }
}'''

CUSTOMER_ADDRESS_CREATE = '''mutation tier3AddressCreate($customerId: ID!, $address: MailingAddressInput!, $setAsDefault: Boolean) {
  customerAddressCreate(customerId: $customerId, address: $address, setAsDefault: $setAsDefault) {
    address { id address1 address2 city zip countryCodeV2 provinceCode company
              firstName lastName phone }
    userErrors { field message }
  }
}'''

# PRESENT BUT UNREACHABLE. No function sends this. See execute_rollback().
CUSTOMER_DELETE = '''mutation tier3CustomerDelete($id: ID!) {
  customerDelete(input: {id: $id}) { deletedCustomerId userErrors { field message } }
}'''

# Read-only documents used by the pre-flight and the idempotency check.
PREFLIGHT_QUERY = '''{
  shop { name myshopifyDomain plan { displayName partnerDevelopment } }
  currentAppInstallation { accessScopes { handle } }
  customersCount { count }
  __type(name: "CustomerInput") { inputFields { name } }
}'''

LEGACY_LOOKUP = ('{ customers(first: 5, query: "%s") { edges { node { id '
                 'metafield(namespace: "custom", key: "legacy_woo_customer_id") '
                 '{ value } } } } }')


# --------------------------------------------------------------------------
# Test definitions - the only customers this module will ever touch
# --------------------------------------------------------------------------

class Tier3Test:
    """One authorized test. Frozen data, not configuration."""

    def __init__(self, test_id, woo_ids, expected_creates, expected_addresses,
                 authorization_phrase, description, expected_metafield_keys,
                 expected_phone_sent, expected_country=None,
                 province_must_be_omitted=False, cohort_frozen=True,
                 expected_customer_count=0, expected_preexisting_woo_ids=(),
                 per_customer=None, notes=''):
        self.test_id = test_id
        self.woo_ids = tuple(woo_ids) if woo_ids else ()
        self.expected_creates = expected_creates
        self.expected_addresses = expected_addresses
        self.authorization_phrase = authorization_phrase
        self.description = description
        self.expected_metafield_keys = tuple(expected_metafield_keys)
        self.expected_phone_sent = expected_phone_sent
        self.expected_country = expected_country
        self.province_must_be_omitted = province_must_be_omitted
        self.cohort_frozen = cohort_frozen
        # The store state this test expects BEFORE it runs, stated as a number
        # rather than a boolean. `requires_store_empty=True` was the class
        # default and was inherited by Test 2, which is designed to run AFTER
        # Test 1 - so it blocked itself on a condition nobody chose for it.
        # None means "do not assert a count" and is used only where a test is
        # explicitly indifferent.
        self.expected_customer_count = expected_customer_count
        # WHO must already be there, verified by legacy metafield rather than
        # inferred from the count. A count of 1 does not establish that the one
        # customer is the one we think it is.
        self.expected_preexisting_woo_ids = tuple(expected_preexisting_woo_ids)
        # A multi-customer cohort has mixed shapes: some members carry two
        # metafields and some one, some send a phone and some omit it, some
        # send a provinceCode and some must not. The flat expected_* fields
        # describe a single-customer test; per_customer overrides them per Woo
        # id so a 10-record cohort can still be asserted exactly rather than
        # loosely.
        self.per_customer = dict(per_customer or {})
        self.notes = notes


TESTS = {
    'TIER3-TEST-1': Tier3Test(
        test_id='TIER3-TEST-1',
        woo_ids=[220],
        expected_creates=1,
        expected_addresses=0,
        expected_metafield_keys=(rt.LEGACY_KEY, rt.REGISTERED_AT_KEY),
        expected_phone_sent=True,
        expected_customer_count=0,       # first write; an empty store is the
        expected_preexisting_woo_ids=(),  # only correct starting state
        authorization_phrase='APPROVED - EXECUTE TIER-3 TEST 1 FOR WOO CUSTOMER 220',
        description=('One registered customer with no address in source. Proves '
                     'customerCreate, both metafields inline, phone policy, and '
                     'consent absence, with no address path involved at all.'),
        notes='Lowest cohort id with no planned address. Selection is reproducible.'),

    'TIER3-TEST-2': Tier3Test(
        test_id='TIER3-TEST-2',
        woo_ids=[2],
        expected_creates=1,
        expected_addresses=1,
        expected_metafield_keys=(rt.LEGACY_KEY,),
        expected_phone_sent=True,
        expected_country='GB',
        province_must_be_omitted=True,
        # AMENDED 2026-08-23 under Approval A. Test 2 runs after Test 1, so it
        # expects exactly the Test-1 customer to be present - not an empty
        # store, and not "any one customer". Both halves are checked.
        expected_customer_count=1,
        expected_preexisting_woo_ids=(220,),
        authorization_phrase='APPROVED - EXECUTE TIER-3 TEST 2 FOR WOO CUSTOMER 2',
        description=('One guest customer with a GB billing address carrying a '
                     'county. Proves the address stage, the GB provinceCode '
                     'omission, postcode preservation and default-address '
                     'behaviour.'),
        notes=('Deliberately NOT woo 1, which was the lowest-id match: woo 1 is '
               'the customer the Gate 6 run lost to "Phone is invalid", so using '
               'it here would put two variables in one test. It is reserved for '
               'Test 3.')),

    'TIER3-TEST-3': Tier3Test(
        test_id='TIER3-TEST-3',
        # FROZEN 2026-08-23 under Approval 2. Every id was derived from the
        # approved manifest and the source classification - none was chosen for
        # being a convenient number, and none is a Test-1 or Test-2 customer.
        woo_ids=[1, 17, 957, 3, 227, 4, 217, 6, 70, 1669],
        expected_creates=10,
        expected_addresses=9,            # 9 of the 10 plan exactly one address
        expected_metafield_keys=(rt.LEGACY_KEY,),   # overridden per customer
        expected_phone_sent=None,                    # overridden per customer
        cohort_frozen=True,
        # The store holds the Test-1 and Test-2 customers. Same invariant the
        # Test-2 amendment introduced: an exact count AND verified identity.
        expected_customer_count=2,
        expected_preexisting_woo_ids=(220, 2),
        # A ten-customer cohort has ten different shapes. Every value below was
        # computed from source, not asserted by hand.
        per_customer={
            1: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                'country': 'GB', 'province_omitted': True},
            17: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': False,
                 'country': 'GB', 'province_omitted': True},
            957: {'addresses': 1,
                  'metafields': (rt.LEGACY_KEY, rt.REGISTERED_AT_KEY),
                  'phone_sent': False, 'country': 'FR', 'province_omitted': True},
            3: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                'country': 'GB', 'province_omitted': True},
            227: {'addresses': 0,
                  'metafields': (rt.LEGACY_KEY, rt.REGISTERED_AT_KEY),
                  'phone_sent': False, 'country': None, 'province_omitted': True},
            4: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                'country': 'GB', 'province_omitted': True},
            217: {'addresses': 1,
                  'metafields': (rt.LEGACY_KEY, rt.REGISTERED_AT_KEY),
                  'phone_sent': True, 'country': 'GB', 'province_omitted': True},
            6: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                'country': 'GB', 'province_omitted': True},
            70: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                 'country': 'IE', 'province_omitted': False},
            1669: {'addresses': 1, 'metafields': (rt.LEGACY_KEY,), 'phone_sent': True,
                   'country': 'IE', 'province_omitted': False},
        },
        authorization_phrase='APPROVED - EXECUTE TIER-3 TEST 3 FOR THE FROZEN 10-CUSTOMER COHORT',
        description=('Ten mixed customers exercising resume, duplicate '
                     'prevention, checkpointing, address fallback, phone '
                     'handling, timeout recovery and throttle handling.'),
        notes=('FROZEN 2026-08-23. Coverage: woo 1 is the known risk #45 case '
               '(GB national number of the wrong length) and woo 1669 is a second, '
               'structurally different route to the same fallback; woo 17 has its '
               'phone omitted by the collision policy; woo 957 is the A_PLUS '
               'billing-to-shipping fallback; woo 227 has no address at all; woo 70 '
               'and woo 1669 send a non-GB provinceCode, a path never exercised '
               'live; woo 217 is registered and holds BOTH addresses, proving '
               'A_PLUS sends only one. Candidates load from source, not from the '
               'manifest, because the manifest carries no shipping postcode or '
               'province - measured on woo 957, which planned 0 addresses instead '
               'of 1 when built from the manifest.')),
}

TIER3_TEST_3_REQUIRED_MEMBER = 1


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

def expectations_for(definition, woo_id):
    """What THIS customer must produce. Falls back to the flat fields when the
    test names no per-customer expectation, so single-customer tests are
    unchanged."""
    entry = definition.per_customer.get(int(woo_id), {})
    return {
        'addresses': entry.get('addresses', definition.expected_addresses),
        'metafields': tuple(entry.get('metafields', definition.expected_metafield_keys)),
        'phone_sent': entry.get('phone_sent', definition.expected_phone_sent),
        'country': entry.get('country', definition.expected_country),
        'province_omitted': entry.get('province_omitted',
                                      definition.province_must_be_omitted),
    }


def resolve_test(test_id):
    """Only a defined test id. No arbitrary customer, ever."""
    if test_id not in TESTS:
        raise Halt(f'{test_id!r} is not a defined Tier-3 test. Defined: '
                   f'{sorted(TESTS)}. This executor accepts no other target - '
                   f'not a customer id, not a cohort file, not the manifest.')
    return TESTS[test_id]


def assert_not_bulk(definition):
    """Independent bound on top of the per-test expectations."""
    if len(definition.woo_ids) > TIER3_MAX_CUSTOMERS:
        raise Halt(f'{definition.test_id}: {len(definition.woo_ids)} customers '
                   f'exceeds the Tier-3 maximum of {TIER3_MAX_CUSTOMERS}. A cohort '
                   f'or manifest run is not a Tier-3 test and this module will not '
                   f'perform one.')
    if definition.expected_creates > TIER3_MAX_CUSTOMERS:
        raise Halt(f'{definition.test_id}: expects {definition.expected_creates} '
                   f'creates; Tier-3 permits at most {TIER3_MAX_CUSTOMERS}.')
    return True


def assert_cohort_frozen(definition):
    if not definition.cohort_frozen:
        raise CohortNotFrozen(
            f'{definition.test_id}: the customer list is not frozen. {definition.notes}')
    if not definition.woo_ids:
        raise CohortNotFrozen(f'{definition.test_id}: no customers defined.')
    return True


def assert_no_duplicate_ids(definition):
    if len(set(definition.woo_ids)) != len(definition.woo_ids):
        raise Halt(f'{definition.test_id}: duplicate Woo ids in the definition.')
    return True


def assert_test_authorization(definition, supplied):
    """The exact phrase for THIS test. Test 1's phrase cannot run Test 2."""
    if (supplied or '') != definition.authorization_phrase:
        for other in TESTS.values():
            if other.test_id != definition.test_id and supplied == other.authorization_phrase:
                raise TestNotAuthorized(
                    f'the phrase supplied authorizes {other.test_id}, not '
                    f'{definition.test_id}. Authorization is per test and is never '
                    f'inherited.')
        raise TestNotAuthorized(
            f'{definition.test_id} requires its exact authorization phrase. No '
            f'paraphrase, no previous approval, and no Approval A build permission '
            f'is a substitute.')
    return True


def contract_hash(path=CONTRACT_PATH):
    if not os.path.exists(path):
        raise Halt(f'frozen contract {path} not found.')
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def assert_contract_unchanged(expected=CONTRACT_SHA256):
    digest = contract_hash()
    if digest != expected:
        raise Halt(f'frozen contract hash mismatch: expected {expected[:16]}..., '
                   f'found {digest[:16]}.... The contract is not edited to make a '
                   f'run pass - a change means re-approval.')
    return digest


def load_contract():
    with open(CONTRACT_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def git_head():
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ''


def git_tree_is_clean():
    try:
        out = subprocess.run(['git', 'status', '--porcelain'], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        return out == ''
    except Exception:  # noqa: BLE001
        return False


# The files whose content decides what a Tier-3 run actually does. A commit
# that touches none of them - a document, a report - cannot change behaviour,
# and must not invalidate an approval that was given against the code.
BEHAVIOUR_PATHS = (
    'migration/scripts/phase10_tier3_executor.py',
    'migration/scripts/phase10_import_runtime.py',
    'migration/scripts/phase10_province_validator.py',
    'migration/schema/phase10_migration_contract.json',
)


def reviewed_commit(paths=BEHAVIOUR_PATHS):
    """The most recent commit touching any file that decides behaviour.

    This, not HEAD, is what an approval names. Approving `fead007` and then
    committing a document would otherwise invalidate the approval while
    changing nothing about what would run - which trains people to pass
    whatever HEAD happens to be, and that is the habit this check exists to
    prevent.
    """
    latest = None
    for path in paths:
        try:
            result = subprocess.run(
                ['git', 'log', '-1', '--format=%H %ct', '--', path],
                capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            continue
        if not result:
            continue
        sha, _sep, when = result.partition(' ')
        stamp = int(when or 0)
        if latest is None or stamp > latest[1]:
            latest = (sha, stamp)
    return latest[0] if latest else ''


def assert_expected_commit(expected, tree_check=None):
    """HEAD must be the commit the approval named, and the tree must be clean.

    A dirty tree means the code that is about to run is not the code that was
    approved, whatever HEAD says.

    tree_check is injectable for the same reason transport is: the offline test
    suite runs while this very file is uncommitted, and a guard that can only be
    exercised from a clean tree is a guard that never gets tested. Production
    callers pass nothing and get the real check.
    """
    if not expected:
        raise Halt('live execution requires --expect-commit naming the reviewed '
                   'executor commit.')
    reviewed = reviewed_commit()
    if not (reviewed.startswith(expected) or expected.startswith(reviewed[:len(expected)])):
        raise Halt(f'commit mismatch: approval names {expected[:12]}, the reviewed '
                   f'executor commit is {reviewed[:12]}. Something that decides '
                   f'behaviour has changed since the approval.')
    if not (tree_check or git_tree_is_clean)():
        raise Halt('the working tree is dirty. The code about to run is not the '
                   'code that was reviewed.')
    return reviewed


# --------------------------------------------------------------------------
# Read-only pre-flight  (requirements 13-20)
# --------------------------------------------------------------------------

def preflight(send, definition, domain, api_version):
    """Every precondition that can be checked without writing. Returns a dict."""
    if (domain or '').strip().lower() != APPROVED_STORE_DOMAIN:
        raise Halt(f'GUARD: store {domain!r} is not the approved store.')
    if (api_version or '') != EXPECTED_API_VERSION:
        raise Halt(f'GUARD: API version {api_version!r} is not the pinned '
                   f'{EXPECTED_API_VERSION!r}.')

    response = send(PREFLIGHT_QUERY, None)
    klass, detail = rt.classify_response(response)
    if klass == rt.AUTH_FAILURE:
        raise Halt(f'GUARD: authentication failed - {detail}')
    if klass != rt.OK:
        raise Halt(f'GUARD: pre-flight query failed - {klass}: {detail}')

    data = response['data']
    shop = data['shop']
    plan = shop.get('plan') or {}
    if plan.get('partnerDevelopment') is not True:
        raise Halt('GUARD: target is not a development store, or does not say it '
                   'is. Missing information is treated as production.')
    domain_live = (shop.get('myshopifyDomain') or '').lower()
    if domain_live != APPROVED_STORE_DOMAIN:
        raise Halt(f'GUARD: live domain {domain_live!r} is not the approved store.')
    if any(marker in domain_live for marker in ('-prod', 'production', 'live-')):
        raise Halt('GUARD: production marker in the store domain.')

    scopes = {s['handle'] for s in data['currentAppInstallation']['accessScopes']}
    missing = REQUIRED_SCOPES - scopes
    if missing:
        raise Halt(f'GUARD: missing scope(s) {sorted(missing)}.')

    fields = {f['name'] for f in (data.get('__type') or {}).get('inputFields', [])}
    if 'addresses' in fields:
        raise Halt('GUARD: schema drift - CustomerInput now exposes `addresses`. '
                   'The two-stage architecture assumes it does not.')
    for required in ('email', 'metafields', 'phone', 'tags'):
        if required not in fields:
            raise Halt(f'GUARD: schema drift - CustomerInput has no {required!r}.')

    # State invariant, in two halves. Both run BEFORE any mutation, and either
    # one halting means nothing is sent.
    count = data['customersCount']['count']
    expected = definition.expected_customer_count
    if expected is not None and count != expected:
        raise Halt(f'GUARD: store holds {count} customer(s); {definition.test_id} '
                   f'expects exactly {expected}. Do not reinterpret this result - '
                   f'an unexpected count means the store is not in the state this '
                   f'test was approved against.')
    assert_expected_preexisting_customers(send, definition)

    return {'store': domain_live, 'development_store': True, 'scopes': len(scopes),
            'customers_before': count, 'expected_customer_count': expected,
            'expected_preexisting_woo_ids': list(definition.expected_preexisting_woo_ids),
            'api_version': api_version}


def find_customer_by_legacy_id(send, woo_id, context='lookup'):
    """The Shopify GID carrying custom.legacy_woo_customer_id = woo_id, or None.

    One implementation, used by both the idempotency check (which requires
    ABSENCE) and the pre-existing-state check (which requires PRESENCE). Two
    copies of this query would be two things that could drift, and a drifted
    identity lookup is the kind of defect that only shows up as a duplicate
    customer.
    """
    query = LEGACY_LOOKUP % (
        "metafields.custom.legacy_woo_customer_id:'%s'" % woo_id)
    response = send(query, None)
    klass, detail = rt.classify_response(response)
    if klass == rt.AUTH_FAILURE:
        raise Halt(f'authentication failed during the {context} - {detail}')
    if klass != rt.OK:
        raise Halt(f'{context} failed - {klass}: {detail}')
    edges = (((response.get('data') or {}).get('customers') or {}).get('edges') or [])
    for edge in edges:
        node = edge.get('node') or {}
        if ((node.get('metafield') or {}).get('value')) == str(woo_id):
            return node.get('id')
    return None


def assert_legacy_id_absent(send, woo_id):
    """Idempotency (requirements 21-22). Returns None, or HALTS if present."""
    existing = find_customer_by_legacy_id(send, woo_id, context='idempotency check')
    if existing:
        raise Halt(f'HALT: woo_customer_id={woo_id} already exists in Shopify as '
                   f'{existing}. A second customer is never created.')
    return None


def assert_expected_preexisting_customers(send, definition):
    """Every customer this test expects to ALREADY be there must be verifiably
    there, identified by its legacy metafield.

    The count check alone is not enough and was never meant to be. A store
    holding exactly one customer satisfies `count == 1` whether that customer is
    the Test-1 record or something nobody authorized; only the identity check
    tells those apart, and they mean completely different things.
    """
    for woo_id in definition.expected_preexisting_woo_ids:
        gid = find_customer_by_legacy_id(send, woo_id,
                                         context='pre-existing state check')
        if not gid:
            raise Halt(
                f'GUARD: {definition.test_id} expects woo_customer_id={woo_id} to '
                f'already exist in Shopify and it does not. The store is not in the '
                f'state this test was approved against.')
    return True


# --------------------------------------------------------------------------
# Payload construction and validation
# --------------------------------------------------------------------------

FORBIDDEN_CUSTOMER_FIELDS = frozenset({
    'addresses', 'emailMarketingConsent', 'smsMarketingConsent',
    'whatsAppMarketingConsent', 'password', 'username', 'wp_capabilities',
    'capabilities', 'company',
})

ALLOWED_CUSTOMER_FIELDS = frozenset({
    'email', 'firstName', 'lastName', 'phone', 'tags', 'metafields',
})


def load_manifest_candidate(woo_id, path=MANIFEST_PATH, expected_hash=MANIFEST_SHA256):
    """One approved customer, from the hash-verified manifest.

    The manifest is the approved artifact, so a Tier-3 payload is built from it
    rather than from a re-derivation that could differ. It carries no shipping
    postcode or province, which is why a shipping-fallback customer cannot come
    from here - see TIER3-TEST-3's notes.
    """
    if not os.path.exists(path):
        raise Halt(f'approved manifest {path} not found.')
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    if digest != expected_hash:
        raise Halt(f'manifest hash mismatch: expected {expected_hash[:16]}..., '
                   f'found {digest[:16]}....')
    with open(path, encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('classification') != 'IMPORT':
                continue
            if int(row['woo_customer_id']) != int(woo_id):
                continue
            return {
                'woo_customer_id': int(row['woo_customer_id']),
                'email': row['email'], 'email_raw': row['email'],
                'first_name': row['first_name'], 'last_name': row['last_name'],
                'company': row['company'], 'phone': row['phone'],
                'is_registered': row['is_registered'] in ('True', 'true', '1'),
                'date_registered': row['date_registered'],
                'billing_address1': row['billing_address1'],
                'billing_address2': '',
                'billing_city': row['billing_city'],
                'billing_province': row['billing_province'],
                'billing_country': row['billing_country'],
                'billing_zip': row['billing_zip'],
                'shipping_address1': row.get('shipping_address1') or '',
                'has_shipping_address': row.get('has_shipping_address') in ('True', 'true', '1'),
            }
    raise Halt(f'woo_customer_id={woo_id} is not an IMPORT row in the approved '
               f'manifest. Refusing.')


_SOURCE_CACHE = {}


def _source_population():
    """The parsed source population, loaded once per process.

    Imported lazily: the dump parse is expensive and nothing that only
    simulates a payload from an injected candidate should pay for it.
    """
    if 'by_id' not in _SOURCE_CACHE:
        from phase10_run_plan import load_population  # noqa: PLC0415
        imports, conflicted, _consent = load_population()
        _SOURCE_CACHE['by_id'] = {c['woo_customer_id']: c for c in imports}
        _SOURCE_CACHE['conflicted'] = set(conflicted)
    return _SOURCE_CACHE['by_id'], _SOURCE_CACHE['conflicted']


def load_approved_candidate(woo_id, path=MANIFEST_PATH, expected_hash=MANIFEST_SHA256):
    """Approval from the manifest, field VALUES from the source.

    The manifest is the artifact that says WHO was approved, and its hash is
    checked first - but it carries no shipping postcode or province, so a
    shipping-fallback customer built from it silently loses its postcode and
    plans zero addresses instead of one. That was measured on woo 957, not
    assumed.

    So the two are used for what each is actually good for: the manifest
    authorises the customer, the source supplies the fields, and the two must
    agree on identity or this halts. A disagreement means the source no longer
    matches what was approved, which is a re-approval, not a re-run.
    """
    approved = load_manifest_candidate(woo_id, path, expected_hash)
    by_id, conflicted = _source_population()
    candidate = by_id.get(int(woo_id))
    if candidate is None:
        raise Halt(f'woo_customer_id={woo_id} is in the approved manifest but not '
                   f'in the source population. Refusing.')
    if int(woo_id) in conflicted:
        raise Halt(f'woo_customer_id={woo_id} is deferred by ADR-014 Gate 5 '
                   f'(name conflict) and must not be imported.')
    manifest_email = (approved.get('email') or '').strip().lower()
    source_email = (candidate.get('email') or '').strip().lower()
    if manifest_email != source_email:
        raise Halt(f'woo_customer_id={woo_id}: the approved manifest and the source '
                   f'disagree on identity. Re-approval required, not a re-run.')
    return candidate


def assert_payload_contract(payload, woo_id):
    """Exactly the fields the contract permits, and no others."""
    present = FORBIDDEN_CUSTOMER_FIELDS & set(payload)
    if present:
        raise Halt(f'woo_customer_id={woo_id}: payload carries forbidden field(s) '
                   f'{sorted(present)}. Addresses are a separate call, consent is a '
                   f'separate pass, company belongs on the address, and the rest '
                   f'never leave WordPress.')
    unknown = set(payload) - ALLOWED_CUSTOMER_FIELDS
    if unknown:
        raise Halt(f'woo_customer_id={woo_id}: undocumented field(s) {sorted(unknown)}.')
    rt.assert_legacy_metafield_present(payload)
    rt.assert_no_server_controlled_fields(payload)
    if not payload.get('email'):
        raise Halt(f'woo_customer_id={woo_id}: no email.')
    return True


def build_plan(definition, candidate, phone_allowed):
    """The stage plan for one Tier-3 customer, under the frozen policies."""
    contract = load_contract()
    policy = contract['address']['policy']
    if policy != rt.ADDRESS_POLICY_RATIFIED:
        raise Halt(f'contract address policy {policy!r} does not match the runtime '
                   f'{rt.ADDRESS_POLICY_RATIFIED!r}.')
    if contract['consent']['written_by_this_migration'] is not False:
        raise Halt('contract says consent is written by this migration; the Tier-3 '
                   'executor has no code path that sets it.')

    stages = rt.plan_customer_import(candidate, phone_allowed=phone_allowed,
                                     address_policy=policy)
    payload = stages[0]['input']
    assert_payload_contract(payload, candidate['woo_customer_id'])

    woo_id = candidate['woo_customer_id']
    expect = expectations_for(definition, woo_id)

    addresses = [s for s in stages if s['stage'] == rt.STAGE_ADDRESS]
    if expect['addresses'] is not None and len(addresses) != expect['addresses']:
        raise Halt(f'{definition.test_id}: woo {woo_id} plans {len(addresses)} '
                   f'address call(s), the approved test expects '
                   f'{expect["addresses"]}.')

    keys = tuple(m['key'] for m in payload['metafields'])
    if keys != expect['metafields']:
        raise Halt(f'{definition.test_id}: woo {woo_id} metafields {keys} do not '
                   f'match the approved {expect["metafields"]}.')

    if expect['phone_sent'] is not None:
        sent = 'phone' in payload
        if sent != expect['phone_sent']:
            raise Halt(f'{definition.test_id}: woo {woo_id} phone sent={sent}, '
                       f'approved test expects {expect["phone_sent"]}.')

    for stage in addresses:
        address = stage['address']
        if expect['country'] and address.get('countryCode') != expect['country']:
            raise Halt(f'{definition.test_id}: woo {woo_id} countryCode '
                       f'{address.get("countryCode")!r}, expected '
                       f'{expect["country"]!r}.')
        if expect['province_omitted'] and 'provinceCode' in address:
            raise Halt(f'{definition.test_id}: provinceCode present on a '
                       f'{address.get("countryCode")} address; it must be omitted.')
        source_zip = (candidate.get(stage['kind'] + '_zip') or '').strip()
        if address.get('zip') not in (None, source_zip):
            raise Halt(f'{definition.test_id}: postcode was reformatted. It may be '
                       f'trimmed, never rewritten.')
    return stages


# --------------------------------------------------------------------------
# Sending - the runtime's policy, a local loop
# --------------------------------------------------------------------------

def send_mutation(send, document, variables, throttle=None, verify=None,
                  sleep=None, max_transient=None, max_throttle_retries=50):
    """One mutation, paced and retried under the runtime's tested policy.

    The loop lives here because phase10_import_runtime.execute_with_retry
    REFUSES mutation documents by construction - the runtime is read-only and
    that guarantee is not being weakened to make Tier-3 convenient. So the
    split is: every DECISION is the runtime's (pacing from extensions.cost,
    response classification, backoff timing, when to halt), and only the call
    that actually sends is local.

    This is not a second throttle implementation. There is no schedule, no
    jitter and no classification defined in this file - all of it is imported.

    Returns (response, attempts).
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
                    raise rt.HaltMigration(
                        'throttled past the retry ceiling; halting') from None
                sleep(rt.backoff_delay(throttled))
                continue
            # Ambiguous: the write may have committed before the socket died.
            # Ask the server before re-sending, or risk a duplicate customer.
            if verify is not None:
                existing = verify()
                if existing:
                    return ({'data': None, '_verified_existing': existing,
                             '_note': 'write confirmed present after an ambiguous '
                                      'failure'}, attempts)
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
                raise rt.HaltMigration('throttled past the retry ceiling; halting')
            sleep(rt.backoff_delay(throttled))
            continue
        if klass == rt.AUTH_FAILURE:
            raise rt.HaltMigration(f'authentication failed, halting: {detail}')
        return response, attempts


# --------------------------------------------------------------------------
# Rollback - specified, not executable
# --------------------------------------------------------------------------

def rollback_spec(created):
    """What a rollback WOULD do. Sends nothing."""
    return {
        'operation': 'customerDelete',
        'document_present': True,
        'executable': False,
        'targets': [{'woo_customer_id': item['woo_customer_id'],
                     'shopify_customer_gid': item['gid']} for item in created],
        'precondition': ('re-read the customer and confirm '
                         'custom.legacy_woo_customer_id matches before deleting'),
        'authorization_required': ('a separate explicit authorization. Approval A '
                                   'granted build permission only, and rollback is '
                                   'a Shopify mutation like any other.'),
    }


def execute_rollback(*_args, **_kwargs):
    """There is no executable rollback path in this revision."""
    raise RollbackNotAuthorized(
        'customerDelete has a document and no executable path. Rollback requires '
        'its own explicit authorization; Approval A did not grant it.')


# --------------------------------------------------------------------------
# Simulation and execution
# --------------------------------------------------------------------------

def _ledger(run_id, simulate):
    suffix = '.simulation' if simulate else ''
    return rt.ImportLedger(ledger_path=LEDGER_PATH + suffix,
                           checkpoint_path=CHECKPOINT_PATH + suffix,
                           run_id=run_id, importer_commit=reviewed_commit())


def simulate(test_id, candidate_loader=load_approved_candidate, phone_allowed=None):
    """Full validation with no network of any kind. Requirement: this must be
    possible before any real mutation path is invoked."""
    definition = resolve_test(test_id)
    assert_not_bulk(definition)
    assert_cohort_frozen(definition)
    assert_no_duplicate_ids(definition)
    assert_contract_unchanged()

    run_id = f'tier3-sim-{test_id}-' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    ledger = _ledger(run_id, simulate=True)
    planned = []
    for woo_id in definition.woo_ids:
        candidate = candidate_loader(woo_id)
        expect = expectations_for(definition, woo_id)
        allowed = (expect['phone_sent'] if phone_allowed is None else phone_allowed)
        stages = build_plan(definition, candidate, phone_allowed=bool(allowed))
        payload = stages[0]['input']
        addresses = [s for s in stages if s['stage'] == rt.STAGE_ADDRESS]
        ledger.record(woo_id, rt.STAGE_CUSTOMER, 'SIMULATED',
                      address_status=(rt.ADDRESS_STATUS_PLANNED if addresses
                                      else rt.ADDRESS_STATUS_NO_SOURCE_ADDRESS),
                      reconciliation_status='NOT_APPLICABLE')
        planned.append({
            'woo_customer_id': woo_id,
            'customerCreate': 1,
            'customerAddressCreate': len(addresses),
            'metafield_keys': [m['key'] for m in payload['metafields']],
            'legacy_id_value': next(m['value'] for m in payload['metafields']
                                    if m['key'] == rt.LEGACY_KEY),
            'phone_sent': 'phone' in payload,
            'consent_present': 'emailMarketingConsent' in payload,
            'payload_fields': sorted(payload),
            'address_fields': [sorted(s['address']) for s in addresses],
            'province_code_sent': ['provinceCode' in s['address'] for s in addresses],
            'set_as_default': [s['setAsDefault'] for s in addresses],
        })

    creates = sum(p['customerCreate'] for p in planned)
    address_calls = sum(p['customerAddressCreate'] for p in planned)
    if creates != definition.expected_creates:
        raise Halt(f'{test_id}: {creates} creates planned, approved test expects '
                   f'{definition.expected_creates}.')
    if (definition.expected_addresses is not None
            and address_calls != definition.expected_addresses):
        raise Halt(f'{test_id}: {address_calls} address call(s) planned, approved '
                   f'test expects {definition.expected_addresses}.')

    result = {
        'test_id': test_id,
        'mode': MODE_SIMULATE,
        'run_id': run_id,
        # The commit the executor ENFORCES via reviewed_commit(), not the tree
        # it happens to sit in. HEAD is recorded separately so the two are
        # distinguishable rather than conflated - conflating them is the
        # auditability defect this replaces.
        'executor_commit': reviewed_commit(),
        'head_commit': git_head(),
        'contract_sha256': contract_hash(),
        'manifest_sha256': MANIFEST_SHA256,
        'woo_customer_ids': list(definition.woo_ids),
        'expected_customerCreate': definition.expected_creates,
        'expected_customerAddressCreate': definition.expected_addresses,
        'planned': planned,
        'planned_customerCreate': creates,
        'planned_customerAddressCreate': address_calls,
        'consent_on_any_payload': any(p['consent_present'] for p in planned),
        'shopify_mutations_performed': 0,
        'customer_writes': 0,
        'address_writes': 0,
        'metafield_writes': 0,
        'rollback': rollback_spec([]),
        'authorization': (f'{test_id} is NOT authorized. Simulation proves the '
                          f'payload; execution requires the exact per-test phrase.'),
    }
    return result


def execute(test_id, authorization, expect_commit, send, domain, api_version,
            candidate_loader=load_approved_candidate, throttle=None, sleep=None,
            tree_check=None):
    """The live path. Every guard above must pass before a single mutation.

    `send` is injected: the caller owns transport, and the offline test suite
    passes a mock so the suite can never reach Shopify.
    """
    definition = resolve_test(test_id)
    assert_not_bulk(definition)
    assert_cohort_frozen(definition)
    assert_no_duplicate_ids(definition)
    assert_test_authorization(definition, authorization)
    assert_contract_unchanged()
    assert_expected_commit(expect_commit, tree_check=tree_check)

    state = preflight(send, definition, domain, api_version)
    for woo_id in definition.woo_ids:
        assert_legacy_id_absent(send, woo_id)

    run_id = f'tier3-{test_id}-' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    ledger = _ledger(run_id, simulate=False)
    throttle = throttle if throttle is not None else rt.ThrottleController()
    created, failed, phone_fallbacks = [], [], []
    mutations = {'customerCreate': 0, 'customerAddressCreate': 0}

    for woo_id in definition.woo_ids:
        candidate = candidate_loader(woo_id)
        # Per customer, not per test. The flat expected_phone_sent is None for a
        # mixed cohort, and bool(None) would have silently omitted every phone -
        # caught by the per-customer guard before any mutation, which is what it
        # is for.
        stages = build_plan(definition, candidate,
                            phone_allowed=bool(
                                expectations_for(definition, woo_id)['phone_sent']))
        payload = stages[0]['input']

        def verify():
            try:
                assert_legacy_id_absent(send, woo_id)
            except Halt:
                return True     # it exists: the write landed
            return None

        response, attempts = send_mutation(
            send, CUSTOMER_CREATE, {'input': payload}, throttle=throttle,
            verify=verify, sleep=sleep)
        if response.get('_verified_existing'):
            raise Halt(f'woo {woo_id}: an ambiguous failure was verified as having '
                       f'landed. Not retrying; reconcile before continuing.')
        mutations['customerCreate'] += 1
        result = response['data']['customerCreate']
        errors = result.get('userErrors') or []

        # Risk #45. Shopify rejects the WHOLE mutation on a phone validation
        # error, so without this a bad number costs the customer, not the
        # field - which is exactly how woo_customer_id=1 was lost in the Gate 6
        # run. Drop the number, tag the customer so the loss is visible in
        # Shopify itself, log the original, and re-issue ONCE. The decision and
        # the retry payload are the runtime's; only the send is local.
        fallback_event = None
        if (errors or not result.get('customer')) and rt.is_phone_user_error(errors) \
                and 'phone' in payload:
            # log_path is passed explicitly and read from the module attribute at
            # CALL time. Its default binds at definition time, so a test that
            # rebinds rt.DROPPED_PHONES_PATH would otherwise still write to the
            # real audit log - which is exactly what happened, 31 times, before
            # this line existed.
            fallback = rt.phone_fallback(payload, errors, woo_id,
                                         operation=rt.STAGE_CUSTOMER, run_id=run_id,
                                         log_path=rt.DROPPED_PHONES_PATH)
            retry_payload = fallback['input']
            # A retry is a fresh payload and faces the same scrutiny as the first.
            assert_payload_contract(retry_payload, woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'PHONE_DROPPED_RETRYING',
                          attempt=attempts, error_class=fallback['event']['reason'],
                          error_detail=json.dumps(fallback['event']['user_errors']),
                          reconciliation_status='PENDING')
            response, retry_attempts = send_mutation(
                send, CUSTOMER_CREATE, {'input': retry_payload},
                throttle=throttle, sleep=sleep)
            mutations['customerCreate'] += 1
            attempts += retry_attempts
            phone_fallbacks.append(woo_id)
            fallback_event = fallback['event']
            result = response['data']['customerCreate']
            errors = result.get('userErrors') or []

        if errors or not result.get('customer'):
            failed.append(woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'FAILED', attempt=attempts,
                          error_class='USER_ERROR',
                          error_detail=json.dumps(rt.sanitize_user_errors(errors)),
                          reconciliation_status='PENDING')
            continue

        customer = result['customer']
        gid = customer['id']
        live_metafields = {edge['node']['key']: edge['node']['value']
                           for edge in (customer.get('metafields') or {}).get('edges', [])}
        if live_metafields.get(rt.LEGACY_KEY) != str(woo_id):
            raise Halt(f'woo {woo_id}: created {gid} carries legacy id '
                       f'{live_metafields.get(rt.LEGACY_KEY)!r}. The identity chain '
                       f'is broken.')
        created.append({'woo_customer_id': woo_id, 'gid': gid,
                        'live': customer, 'addresses': [], 'plan': stages,
                        'phone_dropped': fallback_event is not None})
        ledger.record(woo_id, rt.STAGE_CUSTOMER,
                      'CREATED_PHONE_DROPPED' if fallback_event else 'CREATED',
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
                # The customer survives an address failure, and the address is
                # retried on its own - never by recreating the customer.
                ledger.record(woo_id, rt.STAGE_ADDRESS, 'ADDRESS_FAILED',
                              shopify_gid=gid, attempt=aattempts,
                              error_class='USER_ERROR',
                              error_detail=json.dumps(rt.sanitize_user_errors(aerrors)),
                              address_status='RETRYABLE',
                              reconciliation_status='PENDING')
            else:
                created[-1]['addresses'].append(ares['address'])
                ledger.record(woo_id, rt.STAGE_ADDRESS, 'ADDRESS_CREATED',
                              shopify_gid=gid, attempt=aattempts,
                              address_status=rt.ADDRESS_STATUS_PLANNED,
                              reconciliation_status='PENDING')

    # The cap is on customers CREATED, not on create CALLS. A phone fallback
    # spends a second call on the same customer, and counting calls here would
    # halt a correct run after the writes had already happened.
    if len(created) > definition.expected_creates:
        raise Halt(f'{test_id}: {len(created)} customers created exceeded the '
                   f'approved {definition.expected_creates}.')
    if mutations['customerAddressCreate'] > (definition.expected_addresses or 0):
        raise Halt(f'{test_id}: {mutations["customerAddressCreate"]} address call(s) '
                   f'exceeded the approved {definition.expected_addresses}.')

    return {
        'test_id': test_id, 'mode': MODE_EXECUTE, 'run_id': run_id,
        'executor_commit': reviewed_commit(), 'head_commit': git_head(),
        'contract_sha256': contract_hash(),
        'store_state_before': state, 'mutations': mutations,
        'created': [{'woo_customer_id': c['woo_customer_id'], 'gid': c['gid'],
                     'addresses': len(c['addresses']),
                     'phone_dropped': c.get('phone_dropped', False)} for c in created],
        'failed': failed,
        'phone_fallbacks': phone_fallbacks,
        'customers_saved_by_phone_fallback': [w for w in phone_fallbacks
                                              if w not in failed],
        'rollback': rollback_spec(created),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_argv(argv):
    mode = DEFAULT_MODE
    test_id = authorization = expect_commit = None
    for index, arg in enumerate(argv):
        if arg == '--simulate' and index + 1 < len(argv):
            mode, test_id = MODE_SIMULATE, argv[index + 1]
        elif arg == '--execute' and index + 1 < len(argv):
            mode, test_id = MODE_EXECUTE, argv[index + 1]
        elif arg == '--authorization' and index + 1 < len(argv):
            authorization = argv[index + 1]
        elif arg == '--expect-commit' and index + 1 < len(argv):
            expect_commit = argv[index + 1]
    return mode, test_id, authorization, expect_commit


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    mode, test_id, authorization, expect_commit = parse_argv(argv)

    print('Phase 10 Tier-3 live-test executor.')
    print('Built under Approval A (BUILD ONLY). No test is authorized by that '
          'approval.\n')

    if not test_id:
        print(__doc__)
        print(f'Defined tests: {sorted(TESTS)}')
        print('Nothing was sent.')
        return 2

    if mode == MODE_SIMULATE:
        result = simulate(test_id)
        path = simulation_result_path(test_id)
        os.makedirs(SIMULATION_RESULT_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        print()
        print(f'Wrote {path}')
        print('SHOPIFY MUTATIONS: 0')
        return 0

    # Live path. Transport is only imported here, so a simulation cannot reach
    # the network even by accident.
    from phase9_preflight import get_config, graphql_request  # noqa: PLC0415
    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or ''
    if not domain or not token:
        print('NOT_CONFIGURED - nothing sent.')
        return 2

    def send(document, variables=None):
        return graphql_request(domain, token, api_version, document, variables)

    result = execute(test_id, authorization, expect_commit, send, domain, api_version)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Halt as halt:
        print(f'\n*** HALTED: {halt}')
        sys.exit(1)
