"""Phase 10 customer import runtime - throttling, retry, resume, address
transformation, and PII-safe logging.

THIS MODULE CANNOT PERFORM A SHOPIFY WRITE.

That is a structural property, not a promise. Nothing here builds a GraphQL
mutation document, and every request passes through _guard_document(), which
refuses any document whose operation is a mutation. The executor takes a
caller-supplied `send` callable, so the module has no transport of its own
either. A future importer that wants to write must add its own mutation
documents and deliberately route around this guard.

What it does provide, all of it exercised offline by
test_phase10_import_runtime.py with mocked responses:

  * ThrottleController - proactive pacing from extensions.cost.throttleStatus
  * execute_with_retry  - exponential backoff with jitter, verify-before-retry
                          on ambiguous timeouts, HALT on auth failure
  * fetch_existing_legacy_map - ONE startup scan; the live map is the source of
                          truth for resume, the checkpoint is only an optimisation
  * build_customer_input / build_address_input - deterministic transformation
                          against the API 2026-07 schema contract
  * ImportLedger        - append-and-flush audit ledger carrying no PII
  * sanitize            - redaction applied to every Shopify error before it
                          reaches a log

ARCHITECTURE (ratified 2026-08-21, closed): customerCreate + customerAddressCreate.
The legacy id rides inline in CustomerInput.metafields on the create call;
addresses follow as separate customerAddressCreate calls once a customer id
exists. customerSet is OUT OF SCOPE - not deferred pending evidence - and must
not be reintroduced here without an explicit change request reopening the
decision. Evidence record: docs/PHASE10_CUSTOMER_SET_DECISION.md.
"""
import json
import os
import random
import re
import time

SCHEMA_CONTRACT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schema',
    'shopify_2026_07_contract.json')

REPORTS_DIR = 'reports'
LEDGER_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_import_log.jsonl')
CHECKPOINT_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_import_checkpoint.jsonl')
RESULT_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_import_result.json')

LEGACY_NAMESPACE = 'custom'
LEGACY_KEY = 'legacy_woo_customer_id'
LEGACY_TYPE = 'single_line_text_field'


# --------------------------------------------------------------------------
# Schema contract
# --------------------------------------------------------------------------

def load_schema_contract(path=SCHEMA_CONTRACT_PATH):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def detect_schema_drift(live_input_types, contract=None):
    """Compare a live introspection result against the pinned contract.

    live_input_types: {type_name: {field_name: type_string}} - the same shape
    the contract stores, so a caller can pass either a real introspection
    result or a mocked one. Returns a list of human-readable drift findings;
    empty means the schema still matches what this code was written against.

    This exists because the entire Phase 10 address design was built on
    CustomerInput.addresses, a field that does not exist in 2026-07. That was
    caught by hand. This catches the next one automatically.
    """
    contract = contract or load_schema_contract()
    expected = contract['input_types']
    findings = []
    for type_name, expected_fields in expected.items():
        if type_name not in live_input_types:
            continue
        live_fields = live_input_types[type_name]
        for field, type_str in sorted(expected_fields.items()):
            if field not in live_fields:
                findings.append(f'{type_name}.{field} REMOVED (contract expects {type_str})')
            elif live_fields[field] != type_str:
                findings.append(
                    f'{type_name}.{field} TYPE CHANGED: contract {type_str} -> live {live_fields[field]}')
        for field in sorted(set(live_fields) - set(expected_fields)):
            findings.append(f'{type_name}.{field} ADDED (not in contract)')
    return findings


# --------------------------------------------------------------------------
# PII protection
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'[^\s<>"\']+@[^\s<>"\']+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'\+?[\d][\d\s().-]{6,}\d')
_POSTCODE_RE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', re.IGNORECASE)


def sanitize(text):
    """Redact PII from a string before it reaches any log.

    Shopify echoes submitted values back in userErrors - an invalid-email error
    typically contains the email, an address error the address. The phase 9
    importers log str(userErrors) verbatim, which for customer data would put
    real emails and phone numbers into a log file. Everything logged by this
    module goes through here first.

    Order matters: emails are redacted before phones, because an email
    containing digits would otherwise be partially eaten by the phone pattern.
    """
    if text is None:
        return None
    s = str(text)
    s = _EMAIL_RE.sub('[EMAIL_REDACTED]', s)
    s = _POSTCODE_RE.sub('[POSTCODE_REDACTED]', s)
    s = _PHONE_RE.sub('[PHONE_REDACTED]', s)
    return s


def sanitize_user_errors(user_errors):
    """Shopify UserError is {field, message} - field names are safe, message
    is not. Returns a list of dicts safe to serialise into the ledger."""
    out = []
    for e in user_errors or []:
        if isinstance(e, dict):
            out.append({'field': e.get('field'), 'message': sanitize(e.get('message'))})
        else:
            out.append({'field': None, 'message': sanitize(e)})
    return out


# Only these keys may ever appear in a ledger record. A field carrying customer
# data cannot be added by accident - it has to be added here first, deliberately.
LEDGER_ALLOWED_KEYS = frozenset({
    'woo_customer_id', 'shopify_customer_gid', 'operation', 'status', 'ts',
    'attempt', 'error_class', 'error_detail', 'address_status',
    'reconciliation_status', 'run_id', 'importer_commit',
})

# Never permitted in a ledger record, whatever the value.
LEDGER_FORBIDDEN_KEYS = frozenset({
    'email', 'email_raw', 'first_name', 'last_name', 'phone', 'username',
    'billing_address1', 'billing_address2', 'billing_zip', 'shipping_address1',
    'shipping_address2', 'shipping_zip', 'company', 'password', 'wp_capabilities',
})


def assert_ledger_record_safe(record):
    """Raises if a record carries anything it must not. Called on every write."""
    bad = LEDGER_FORBIDDEN_KEYS & set(record)
    if bad:
        raise ValueError(f'ledger record carries PII fields: {sorted(bad)}')
    unknown = set(record) - LEDGER_ALLOWED_KEYS
    if unknown:
        raise ValueError(f'ledger record carries unapproved fields: {sorted(unknown)}')
    return True


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------

class ThrottleController:
    """Proactive pacing from Shopify's own cost extension.

    Measured live on this store: maximumAvailable 2000, restoreRate 100/s, a
    trivial read costing 1.

    MUTATION COST IS NOW MEASURED, not assumed (2026-08-22, Step 9): customerCreate,
    customerUpdate and customerAddressCreate each cost requested 10 / actual 10.
    Measured without creating anything, by sending mutations Shopify rejects at
    field validation - those still execute as GraphQL operations and return
    extensions.cost. See reports/phase10_mutation_cost_analysis.json.

    So assumed_cost=10 is correct rather than merely conventional, and the
    earlier instruction to re-measure it on the first real mutation is
    discharged. At 100 points/s that is 10 mutations/s sustained, with a burst
    of 200 from a full bucket.

    The point is to never hit THROTTLED at all. Reacting to throttling is the
    fallback; pacing below the floor is the strategy.
    """

    def __init__(self, floor=500, assumed_cost=10, sleep=None, clock=None):
        self.floor = floor
        self.assumed_cost = assumed_cost
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.currently_available = None
        self.maximum_available = None
        self.restore_rate = None
        self.last_actual_cost = None
        self.observations = 0
        self.pauses = 0
        self.total_paused_seconds = 0.0

    def observe(self, response):
        """Read extensions.cost from a response. Tolerates its absence -
        Shopify omits extensions on some error paths, and a missing cost block
        must not crash an import."""
        cost = ((response or {}).get('extensions') or {}).get('cost') or {}
        status = cost.get('throttleStatus') or {}
        if 'currentlyAvailable' in status:
            self.currently_available = status['currentlyAvailable']
        if 'maximumAvailable' in status:
            self.maximum_available = status['maximumAvailable']
        if 'restoreRate' in status:
            self.restore_rate = status['restoreRate']
        if 'actualQueryCost' in cost and cost['actualQueryCost'] is not None:
            self.last_actual_cost = cost['actualQueryCost']
        self.observations += 1
        return self

    def seconds_to_recover(self, target=None):
        """How long until the bucket holds `target` points."""
        if self.currently_available is None or not self.restore_rate:
            return 0.0
        target = self.floor if target is None else target
        if self.currently_available >= target:
            return 0.0
        return (target - self.currently_available) / float(self.restore_rate)

    def pace(self):
        """Sleep if the bucket has fallen below the floor. Returns the number
        of seconds slept, so tests can assert on it without real time passing."""
        wait = self.seconds_to_recover()
        if wait <= 0:
            return 0.0
        self.pauses += 1
        self.total_paused_seconds += wait
        self._sleep(wait)
        # Assume the bucket refilled to the floor over that sleep.
        self.currently_available = self.floor
        return wait

    def can_afford(self, cost=None):
        cost = self.assumed_cost if cost is None else cost
        if self.currently_available is None:
            return True
        return self.currently_available >= cost


# --------------------------------------------------------------------------
# Retry / backoff
# --------------------------------------------------------------------------

BACKOFF_SCHEDULE = (1, 2, 4, 8, 16)
MAX_TRANSIENT_ATTEMPTS = 3


def backoff_delay(attempt, jitter=None):
    """Delay for a 1-indexed attempt number, with jitter.

    Jitter is proportional (up to +25%) rather than absolute, so the spread
    scales with the wait. Attempts beyond the schedule clamp at its last value
    rather than growing without bound - a throttled import should keep trying
    at a steady 16s, not drift towards minutes.
    """
    idx = min(max(attempt, 1), len(BACKOFF_SCHEDULE)) - 1
    base = BACKOFF_SCHEDULE[idx]
    jitter = random.random if jitter is None else jitter
    return base * (1.0 + 0.25 * jitter())


class HaltMigration(RuntimeError):
    """Unrecoverable. The run must stop, loudly, without further requests.

    Reserved for conditions where continuing would be dishonest or damaging:
    an expired token (every subsequent record would 'fail' for a reason that
    has nothing to do with the record), or a duplicate legacy ID (a source-data
    invariant is broken and the resume design's key is not unique after all).
    """


# Failure classes. THROTTLED is deliberately NOT a failure - see execute_with_retry.
OK = 'OK'
THROTTLED = 'THROTTLED'
TRANSIENT = 'TRANSIENT'
USER_ERROR = 'USER_ERROR'
AUTH_FAILURE = 'AUTH_FAILURE'
GRAPHQL_ERROR = 'GRAPHQL_ERROR'


def classify_response(response):
    """Classify a GraphQL response body. Returns (class, sanitized_detail)."""
    if not isinstance(response, dict):
        return TRANSIENT, 'non-dict response'
    errors = response.get('errors')
    if errors:
        codes = {(e.get('extensions') or {}).get('code') for e in errors if isinstance(e, dict)}
        if 'THROTTLED' in codes:
            return THROTTLED, 'THROTTLED'
        if 'ACCESS_DENIED' in codes or 'UNAUTHENTICATED' in codes:
            return AUTH_FAILURE, sanitize('; '.join(str(e.get('message', e)) for e in errors))
        return GRAPHQL_ERROR, sanitize('; '.join(str(e.get('message', e)) for e in errors))
    return OK, None


def classify_exception(exc):
    """Classify a transport-level exception. Returns (class, sanitized_detail)."""
    text = f'{type(exc).__name__}: {exc}'
    lowered = text.lower()
    if '401' in text or 'unauthorized' in lowered or 'invalid api key' in lowered:
        return AUTH_FAILURE, sanitize(text)
    if '429' in text or 'throttl' in lowered:
        return THROTTLED, sanitize(text)
    return TRANSIENT, sanitize(text)


_MUTATION_OPERATION_RE = re.compile(r'^\s*mutation\b')


def _guard_document(document):
    """Structural refusal of any mutation. See the module docstring."""
    if _MUTATION_OPERATION_RE.match(document or ''):
        raise HaltMigration(
            'phase10_import_runtime refused a mutation document: this module is '
            'read-only by construction and is not authorized to write to Shopify')
    return document


def execute_with_retry(send, document, variables=None, throttle=None, verify=None,
                       sleep=None, jitter=None, max_transient=MAX_TRANSIENT_ATTEMPTS,
                       max_throttle_retries=50):
    """Run one request with pacing, backoff, and verify-before-retry.

    send      - callable(document, variables) -> response dict. Supplied by the
                caller; this module owns no transport.
    verify    - optional callable() -> gid or None. Called after an ambiguous
                failure (timeout / transport error) to ask the server whether
                the write actually landed. This is what stops a retry from
                creating a duplicate: a timeout is genuinely ambiguous, the
                mutation may have committed before the connection dropped.

    Returns (response, attempts).

    Throttling never consumes the transient budget. A run that is merely being
    paced is not a run that is failing, and conflating the two causes healthy
    records to be quarantined under load - exactly when quarantining is most
    expensive to unpick.
    """
    _guard_document(document)
    sleep = sleep or time.sleep
    transient_attempts = 0
    throttle_attempts = 0
    attempts = 0

    while True:
        if throttle is not None:
            throttle.pace()
        attempts += 1
        try:
            response = send(document, variables)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            klass, detail = classify_exception(exc)
            if klass == AUTH_FAILURE:
                raise HaltMigration(f'authentication failed, halting: {detail}') from None
            if klass == THROTTLED:
                throttle_attempts += 1
                if throttle_attempts > max_throttle_retries:
                    raise HaltMigration('throttled past the retry ceiling; halting') from None
                sleep(backoff_delay(throttle_attempts, jitter))
                continue
            # Ambiguous: the write may have landed. Ask before retrying.
            if verify is not None:
                existing = verify()
                if existing:
                    return {'data': None, '_verified_existing': existing,
                            '_note': 'write confirmed present after ambiguous failure'}, attempts
            transient_attempts += 1
            if transient_attempts >= max_transient:
                raise
            sleep(backoff_delay(transient_attempts, jitter))
            continue

        if throttle is not None:
            throttle.observe(response)
        klass, detail = classify_response(response)

        if klass == THROTTLED:
            throttle_attempts += 1
            if throttle_attempts > max_throttle_retries:
                raise HaltMigration('throttled past the retry ceiling; halting')
            sleep(backoff_delay(throttle_attempts, jitter))
            continue
        if klass == AUTH_FAILURE:
            raise HaltMigration(f'authentication failed, halting: {detail}')
        return response, attempts


# --------------------------------------------------------------------------
# Legacy ID map - the source of truth for resume
# --------------------------------------------------------------------------

LEGACY_MAP_QUERY = (
    '{ customers(first: %(page)d%(after)s) { pageInfo { hasNextPage endCursor } '
    'edges { node { id addresses { id } '
    'metafield(namespace: "%(ns)s", key: "%(key)s") { value } } } } }')


def build_legacy_map_query(page_size=250, cursor=None):
    after = ', after: "%s"' % cursor if cursor else ''
    return LEGACY_MAP_QUERY % {'page': page_size, 'after': after,
                               'ns': LEGACY_NAMESPACE, 'key': LEGACY_KEY}


def fetch_existing_legacy_map(send, page_size=250, throttle=None, sleep=None, jitter=None):
    """ONE startup scan building {woo_customer_id: {gid, address_count}}.

    This is the source of truth for what has already been imported. The
    checkpoint file is an audit artifact and a speed optimisation - correctness
    never depends on it, so a deleted or corrupted checkpoint costs one extra
    scan and nothing else.

    Deliberately NOT the per-record find_existing_by_legacy_id() pattern from
    phase9_test_import.py:184, which pages the whole collection for every
    record. That is quadratic: at 12,096 customers it is roughly 12,096 x 49
    pages, around 590,000 reads, hours of lookup before a single write. It was
    written for a five-product test set and must not be carried forward.

    Raises HaltMigration if one Woo ID maps to two Shopify customers - the
    resume design's uniqueness assumption is broken and continuing would
    silently pick a winner.
    """
    mapping = {}
    cursor = None
    pages = 0
    while True:
        document = build_legacy_map_query(page_size, cursor)
        response, _ = execute_with_retry(send, document, throttle=throttle,
                                         sleep=sleep, jitter=jitter)
        pages += 1
        conn = ((response.get('data') or {}).get('customers') or {})
        for edge in conn.get('edges') or []:
            node = edge.get('node') or {}
            mf = node.get('metafield')
            if not mf or mf.get('value') in (None, ''):
                continue
            woo_id = str(mf['value'])
            gid = node.get('id')
            if woo_id in mapping and mapping[woo_id]['gid'] != gid:
                raise HaltMigration(
                    f'duplicate legacy Woo customer id {woo_id} maps to two Shopify '
                    f'customers; halting rather than choosing one')
            mapping[woo_id] = {'gid': gid,
                               'address_count': len(node.get('addresses') or [])}
        page = conn.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            return mapping, pages
        cursor = page.get('endCursor')


def records_to_process(manifest_woo_ids, legacy_map):
    """Resume: everything in the manifest not already live. Pure function of
    the live map - the checkpoint is not consulted."""
    done = set(legacy_map)
    return [i for i in manifest_woo_ids if str(i) not in done]


def partial_address_customers(legacy_map, expected_address_counts):
    """Customers that exist but are short of their expected addresses -
    the 'customer created, address failed' state, detected from live data
    rather than inferred from a log."""
    out = []
    for woo_id, expected in expected_address_counts.items():
        entry = legacy_map.get(str(woo_id))
        if entry is not None and entry.get('address_count', 0) < expected:
            out.append(str(woo_id))
    return sorted(out)


# --------------------------------------------------------------------------
# Input construction - API 2026-07 verified shapes
# --------------------------------------------------------------------------

def _load_country_codes():
    try:
        return set(load_schema_contract()['country_codes'])
    except Exception:  # noqa: BLE001 - contract is optional for pure-transform use
        return set()


COUNTRY_CODES = _load_country_codes()

# SUPERSEDED 2026-08-21 by validate_province_code(), which checks the VALUE
# against Shopify's own accepted codes rather than gating on the country. The
# old allowlist excluded Ireland (26 real Shopify counties) and would have sent
# any string a listed country carried. Do not reintroduce a country-only gate.


def build_customer_input(candidate, include_registered_at=True, phone_allowed=True):
    """Build CustomerInput for customerCreate.

    Verified against API 2026-07: CustomerInput accepts email,
    emailMarketingConsent, firstName, id, lastName, locale, metafields,
    multipassIdentifier, note, phone, smsMarketingConsent, tags, taxExempt,
    taxExemptions, whatsAppMarketingConsent.

    Three consequences that earlier documentation got wrong:
      * there is NO addresses field - addresses are a separate stage
      * there is NO company field - company belongs on MailingAddressInput
      * date_registered has no native destination - Customer.createdAt is
        server-set and read-only, so it can only be a metafield

    emailMarketingConsent is never set. Per docs/PHASE10_GDPR_CONSENT.md the
    standing policy is to omit it for all 12,096 customers pending sign-off,
    and this function has no parameter to override that.
    """
    tags = ['imported-from-woocommerce',
            'registered' if candidate.get('is_registered') else 'guest']

    # Mandatory - ratified 2026-08-21. There is deliberately no parameter to
    # omit this: see assert_legacy_metafield_present() for why one customer
    # without it is worse than it looks.
    metafields = [legacy_metafield(candidate['woo_customer_id'])]
    if include_registered_at:
        registered = registered_at_metafield(candidate.get('date_registered'))
        if registered is not None:
            metafields.append(registered)

    payload = {'email': candidate['email'], 'tags': tags, 'metafields': metafields}
    if candidate.get('first_name'):
        payload['firstName'] = candidate['first_name']
    if candidate.get('last_name'):
        payload['lastName'] = candidate['last_name']
    # phone_allowed is driven by the collision review: Shopify enforces
    # store-wide phone uniqueness, and 517 customers across 240 groups share a
    # number with someone else. A customer whose group is unresolved, or who
    # is not the reviewer-selected owner, is still created - just without this
    # field. See phone_owner_map().
    if phone_allowed and (candidate.get('phone') or '').strip():
        payload['phone'] = candidate['phone'].strip()
    return payload


# Shopify's CountryCode enum includes ZZ ("Unknown Region"), which is accepted
# by the API but is not a country. An address carrying it is not deliverable and
# should reach a human, not Shopify - so it is refused here even though the enum
# would allow it.
NON_COUNTRY_CODES = frozenset({'ZZ'})

ADDRESS_SKIP_NO_STREET = 'NO_ADDRESS1'
ADDRESS_SKIP_NO_COUNTRY = 'MISSING_COUNTRY'
ADDRESS_SKIP_BAD_COUNTRY = 'INVALID_COUNTRY_CODE'
ADDRESS_SKIP_UNKNOWN_REGION = 'COUNTRY_CODE_IS_UNKNOWN_REGION'
ADDRESS_FLAG_NO_CITY = 'MISSING_CITY'
ADDRESS_FLAG_NO_ZIP = 'MISSING_ZIP'
ADDRESS_FLAG_PROVINCE_DROPPED = 'PROVINCE_DROPPED_COUNTRY_HAS_NO_PROVINCES'


def build_address_input(candidate, kind='billing', country_codes=None):
    """Build MailingAddressInput, or explain why no address is possible.

    Returns (address_dict_or_None, flags). flags is a list of reason codes:
    a returned None always carries a skip reason; a returned address may still
    carry advisory flags.

    Verified MailingAddressInput fields (API 2026-07), complete list:
        address1, address2, city, company, countryCode, firstName, lastName,
        phone, provinceCode, zip

    Note what is absent: no free-text `country`, no free-text `province`, no
    `name`. Documentation referencing CustomerAddressInput with country/province
    is wrong on both the type name and the fields.
    """
    codes = COUNTRY_CODES if country_codes is None else country_codes
    prefix = 'billing_' if kind == 'billing' else 'shipping_'
    flags = []

    street = (candidate.get(prefix + 'address1') or '').strip()
    if not street:
        return None, [ADDRESS_SKIP_NO_STREET]

    country = (candidate.get(prefix + 'country') or '').strip().upper()
    if not country:
        return None, [ADDRESS_SKIP_NO_COUNTRY]
    if codes and country not in codes:
        return None, [ADDRESS_SKIP_BAD_COUNTRY]
    if country in NON_COUNTRY_CODES:
        return None, [ADDRESS_SKIP_UNKNOWN_REGION]

    address = {'address1': street, 'countryCode': country}

    address2 = (candidate.get(prefix + 'address2') or '').strip()
    if address2:
        address['address2'] = address2

    city = (candidate.get(prefix + 'city') or '').strip()
    if city:
        address['city'] = city
    else:
        flags.append(ADDRESS_FLAG_NO_CITY)

    # Postcode: trim only, never reformat. Malformed UK postcodes are already
    # recorded as informational by the dry run and Shopify does not validate
    # them; "correcting" them risks corrupting valid data to fix a cosmetic
    # complaint.
    zip_code = (candidate.get(prefix + 'zip') or '').strip()
    if zip_code:
        address['zip'] = zip_code
    else:
        flags.append(ADDRESS_FLAG_NO_ZIP)

    province = (candidate.get(prefix + 'province') or '').strip()
    if province:
        code, province_flag = validate_province_code(country, province)
        if code is not None:
            address['provinceCode'] = code
        if province_flag and province_flag != PROVINCE_SENT:
            flags.append(province_flag)

    company = (candidate.get('company') or '').strip()
    if company:
        address['company'] = company

    phone = (candidate.get('phone') or '').strip()
    if phone:
        address['phone'] = phone

    if candidate.get('first_name'):
        address['firstName'] = candidate['first_name']
    if candidate.get('last_name'):
        address['lastName'] = candidate['last_name']

    return address, flags


# Address policy - ADR-014 Gate 2, decided 2026-08-22.
#
#   A       billing only
#   B       billing + shipping
#   A_PLUS  billing, falling back to shipping for a customer who has no
#           billing address  <- SELECTED
#
# A_PLUS exists because A and B were not the only two shapes available. Option
# A would have given no address at all to the customers whose only usable
# address is a shipping address, while their data sat unused in the source;
# option B would have given 1,193 customers a second address that Shopify
# renders with no billing/shipping label, which is an operational cost paid by
# staff every time they look at one of those records. A_PLUS takes the first
# address a customer actually has and stops - at most one address each, and
# nobody with address data ends up with none.
ADDRESS_POLICY_BILLING_ONLY = 'A_BILLING_ONLY'
ADDRESS_POLICY_BILLING_PLUS_SHIPPING = 'B_BILLING_PLUS_SHIPPING'
ADDRESS_POLICY_BILLING_ELSE_SHIPPING = 'A_PLUS_BILLING_ELSE_SHIPPING'

ADDRESS_POLICIES = (ADDRESS_POLICY_BILLING_ONLY,
                    ADDRESS_POLICY_BILLING_PLUS_SHIPPING,
                    ADDRESS_POLICY_BILLING_ELSE_SHIPPING)

# The ratified selection. Not a default in the sense of "what happens if nobody
# decided" - it is what was decided, recorded here so a caller cannot silently
# run a different policy than the one signed off.
ADDRESS_POLICY_RATIFIED = ADDRESS_POLICY_BILLING_ELSE_SHIPPING


class UnknownAddressPolicy(ValueError):
    """A policy string nothing implements. Raised rather than falling back to
    one of the real policies, which would import thousands of customers under a
    rule nobody chose."""


def plan_addresses(candidate, include_shipping=False, country_codes=None,
                   policy=None):
    """Ordered address plan for one customer.

    policy selects between the three shapes above. include_shipping is the
    original two-way switch and is kept working - it maps to A and B - so the
    callers and tests written before Gate 2 closed still mean what they said.
    Passing policy explicitly wins over it.

    The first address planned gets setAsDefault=True, so a customer whose only
    address is a shipping address gets that address as their default rather
    than a defaultless record.
    """
    if policy is None:
        policy = (ADDRESS_POLICY_BILLING_PLUS_SHIPPING if include_shipping
                  else ADDRESS_POLICY_BILLING_ONLY)
    if policy not in ADDRESS_POLICIES:
        raise UnknownAddressPolicy(
            f'{policy!r} is not an implemented address policy; expected one of '
            f'{list(ADDRESS_POLICIES)}')

    plan = []
    billing, billing_flags = build_address_input(candidate, 'billing', country_codes)
    if billing:
        plan.append({'kind': 'billing', 'address': billing,
                     'setAsDefault': True, 'flags': billing_flags})

    wants_shipping = (
        policy == ADDRESS_POLICY_BILLING_PLUS_SHIPPING
        # A_PLUS reaches for the shipping address only when billing produced
        # nothing - including when billing was SKIPPED rather than absent, which
        # is the whole point: those customers have usable data and would
        # otherwise import with no address.
        or (policy == ADDRESS_POLICY_BILLING_ELSE_SHIPPING and not plan))
    if wants_shipping:
        shipping, shipping_flags = build_address_input(candidate, 'shipping', country_codes)
        if shipping:
            plan.append({'kind': 'shipping', 'address': shipping,
                         'setAsDefault': not plan, 'flags': shipping_flags})
    return plan


# --------------------------------------------------------------------------
# Audit ledger
# --------------------------------------------------------------------------

class ImportLedger:
    """Append-and-flush audit ledger plus resume checkpoint.

    Every record is flushed and fsynced immediately. Buffering would mean a
    killed process loses exactly the records it was least sure about. The cost
    is real but irrelevant next to a 28-minute run.

    Both files carry Shopify GIDs tied to real people and must stay gitignored;
    only the aggregate result JSON is safe to track.
    """

    def __init__(self, ledger_path=LEDGER_PATH, checkpoint_path=CHECKPOINT_PATH,
                 run_id=None, importer_commit=None, now=None):
        self.ledger_path = ledger_path
        self.checkpoint_path = checkpoint_path
        self.run_id = run_id
        self.importer_commit = importer_commit
        self._now = now or (lambda: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        self.counts = {}

    @staticmethod
    def _append(path, payload):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, sort_keys=True) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def record(self, woo_customer_id, operation, status, shopify_gid=None,
               attempt=1, error_class=None, error_detail=None,
               address_status=None, reconciliation_status='PENDING'):
        entry = {
            'woo_customer_id': int(woo_customer_id),
            'shopify_customer_gid': shopify_gid,
            'operation': operation,
            'status': status,
            'ts': self._now(),
            'attempt': attempt,
            'error_class': error_class,
            'error_detail': sanitize(error_detail),
            'address_status': address_status,
            'reconciliation_status': reconciliation_status,
            'run_id': self.run_id,
            'importer_commit': self.importer_commit,
        }
        assert_ledger_record_safe(entry)
        self._append(self.ledger_path, entry)
        if shopify_gid:
            self._append(self.checkpoint_path,
                         {'woo_id': int(woo_customer_id), 'gid': shopify_gid,
                          'action': status})
        self.counts[status] = self.counts.get(status, 0) + 1
        return entry

    def write_result(self, path=RESULT_PATH, extra=None):
        """Aggregate counts only - this file is tracked in git and must never
        gain a per-customer field."""
        result = {'run_id': self.run_id, 'importer_commit': self.importer_commit,
                  'counts': dict(self.counts)}
        result.update(extra or {})
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, sort_keys=True)
        return result


# --------------------------------------------------------------------------
# Phone uniqueness
# --------------------------------------------------------------------------

def phone_digits(phone):
    return re.sub(r'\D', '', phone or '')


def phone_canonical(phone):
    """Best-effort canonical form for COLLISION DETECTION ONLY.

    Never used to rewrite a stored value. Shopify normalises phone numbers
    internally, so 07700900123 and +447700900123 are the same subscriber and
    will collide on write even though they differ as strings. Detecting that
    before the import is the whole point; "fixing" the source data is not.
    """
    d = phone_digits(phone)
    if d.startswith('440'):
        d = '44' + d[3:]
    elif d.startswith('0') and len(d) == 11:
        d = '44' + d[1:]
    return d


def phone_collision_groups(candidates, canonicalize=None):
    """{canonical_form: [woo_customer_id, ...]} for every form used more than
    once. Returns Woo IDs only - never a phone number.

    Shopify enforces Customer.phone uniqueness store-wide, but the Phase 10 dry
    run deduplicates on email alone, so a phone shared by two distinct
    customers is invisible until the second write fails.
    """
    canonicalize = canonicalize or phone_canonical
    groups = {}
    for c in candidates:
        key = canonicalize(c.get('phone'))
        if not key:
            continue
        groups.setdefault(key, []).append(c['woo_customer_id'])
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def phone_collision_summary(candidates, canonicalize=None):
    """Aggregate counts only - safe to write to a tracked report."""
    with_phone = [c for c in candidates if (c.get('phone') or '').strip()]
    groups = phone_collision_groups(with_phone, canonicalize)
    affected = sum(len(v) for v in groups.values())
    return {
        'customers_with_phone': len(with_phone),
        'collision_groups': len(groups),
        'customers_in_collisions': affected,
        'max_group_size': max((len(v) for v in groups.values()), default=0),
        'customers_with_unique_phone': len(with_phone) - affected,
    }


# --------------------------------------------------------------------------
# Phone collision resolution
# --------------------------------------------------------------------------

PHONE_HASH_SALT_ENV = 'PHASE10_PHONE_HASH_SALT'
DEFAULT_PHONE_HASH_SALT = 'phase10-collision-grouping'

# Decisions a reviewer may record against a collision group.
ACTION_KEEP_ONE = 'KEEP_ONE'
ACTION_OMIT_FROM_ALL = 'OMIT_FROM_ALL'
ACTION_MANUAL_REVIEW = 'MANUAL_REVIEW_REQUIRED'

REVIEW_PENDING = 'PENDING'
REVIEW_RESOLVED = 'RESOLVED'

# A group this large is almost certainly a shop or placeholder number rather
# than a shared household line, and assigning it to any one customer would be
# a guess dressed up as a decision.
HIGH_RISK_GROUP_SIZE = 10


def phone_hash(phone, salt=None):
    """Stable, privacy-safe label for a canonical phone number.

    HONEST LIMITATION, stated because it matters: a phone number lives in a
    keyspace of roughly 10^10, so a hash with a known salt is brute-forceable
    by anyone holding this file and this code. Set PHASE10_PHONE_HASH_SALT to
    a secret value if the report is ever shared outside the team.

    Even with the default salt, the hash is not the control protecting the
    number - the control is that no raw number is written anywhere tracked.
    The hash gives reviewers a stable group label; the actionable field is
    source_customer_ids, and the number itself is looked up in the gitignored
    manifest, never here.
    """
    import hashlib
    import hmac
    key = (salt if salt is not None else
           os.environ.get(PHONE_HASH_SALT_ENV, DEFAULT_PHONE_HASH_SALT))
    canonical = phone_canonical(phone)
    if not canonical:
        return None
    return hmac.new(key.encode('utf-8'), canonical.encode('utf-8'),
                    hashlib.sha256).hexdigest()[:16]


def build_collision_report(candidates, salt=None, canonicalize=None):
    """One row per collision group. Deterministic: identical input yields
    identical output, including group ordering and ids.

    recommended_action is MANUAL_REVIEW_REQUIRED for every group. No rule in
    this dataset establishes who owns a shared number - neither WooCommerce
    nor the Phase 10 mapping defines phone ownership - so selecting a winner
    would be inventing an ownership rule, not applying one. The report gives
    reviewers the evidence (registered versus guest counts) without making
    the call for them.
    """
    canonicalize = canonicalize or phone_canonical
    with_phone = [c for c in candidates if (c.get('phone') or '').strip()]
    by_key = {}
    for c in with_phone:
        key = canonicalize(c.get('phone'))
        if key:
            by_key.setdefault(key, []).append(c)

    groups = {k: v for k, v in by_key.items() if len(v) > 1}
    rows = []
    # Ordered by lowest Woo id so group ids stay stable across runs.
    for index, key in enumerate(sorted(groups, key=lambda k: min(
            c['woo_customer_id'] for c in groups[k])), 1):
        members = sorted(groups[key], key=lambda c: c['woo_customer_id'])
        ids = [c['woo_customer_id'] for c in members]
        registered = sum(1 for c in members if c.get('is_registered'))
        rows.append({
            'collision_group_id': index,
            'normalized_phone_hash': phone_hash(members[0].get('phone'), salt),
            'affected_customer_count': len(ids),
            'source_customer_ids': ' '.join(str(i) for i in ids),
            'registered_accounts_in_group': registered,
            'guest_rows_in_group': len(ids) - registered,
            'risk': 'HIGH' if len(ids) >= HIGH_RISK_GROUP_SIZE else 'NORMAL',
            'available_decisions': ACTION_KEEP_ONE + '|' + ACTION_OMIT_FROM_ALL + '|' + ACTION_MANUAL_REVIEW,
            'recommended_action': ACTION_MANUAL_REVIEW,
            'chosen_owner_woo_customer_id': '',
            'review_status': REVIEW_PENDING,
        })
    return rows


def phone_owner_map(collision_rows, decisions=None):
    """{woo_customer_id: (include_phone, reason)} for every colliding customer.

    decisions maps collision_group_id -> {'action': ..., 'chosen_owner': ...}
    and normally comes from a reviewed CSV. Absent a decision, every member of
    the group has their phone OMITTED - the safe default, because sending a
    duplicate phone fails the write for whichever customer Shopify happens to
    see second, making the outcome depend on ordering rather than on intent.

    Omitting a phone never blocks a customer: they are still created, without
    that one field.
    """
    decisions = decisions or {}
    out = {}
    for row in collision_rows:
        gid = row['collision_group_id']
        ids = [int(i) for i in str(row['source_customer_ids']).split()]
        decision = decisions.get(gid) or {}
        action = decision.get('action', row.get('recommended_action', ACTION_MANUAL_REVIEW))

        if action == ACTION_KEEP_ONE:
            owner = decision.get('chosen_owner')
            if owner is None or int(owner) not in ids:
                raise ValueError(
                    'collision group %s: KEEP_ONE requires chosen_owner to be one of '
                    'the group members, got %r' % (gid, owner))
            owner = int(owner)
            for i in ids:
                out[i] = (i == owner,
                          'reviewer-selected owner' if i == owner
                          else 'phone omitted; group %s owner is %s' % (gid, owner))
        elif action == ACTION_OMIT_FROM_ALL:
            for i in ids:
                out[i] = (False, 'phone omitted from every member of group %s' % gid)
        else:
            for i in ids:
                out[i] = (False, 'group %s unresolved (%s); phone omitted pending review'
                          % (gid, ACTION_MANUAL_REVIEW))
    return out


def unresolved_collision_groups(collision_rows, decisions=None):
    decisions = decisions or {}
    return [r['collision_group_id'] for r in collision_rows
            if (decisions.get(r['collision_group_id']) or {}).get(
                'action', r.get('recommended_action')) == ACTION_MANUAL_REVIEW]


# --------------------------------------------------------------------------
# Phone write-failure fallback  (risk #45, 2026-08-22)
# --------------------------------------------------------------------------
#
# Found by the Gate 6 test import, not by any offline check: woo_customer_id=1
# came back as
#
#     userErrors: [{field: ["phone"], message: "Phone is invalid"}]
#
# and NO CUSTOMER WAS CREATED. Shopify rejects the whole mutation, not the
# offending field, so on the customerCreate path a bad phone number does not
# cost a phone number - it costs a customer. Across 4,450 customers carrying a
# phone that is an unbounded silent-loss channel.
#
# The design review already specified the correct handling ("invalid phone:
# RETRY once without phone, then QUARANTINE"); it had simply never been built.
# This section builds it.
#
# WHAT THIS MODULE DOES AND DOES NOT DO
# -------------------------------------
# It decides and it transforms. It does not send. phase10_import_runtime is
# read-only by construction - _guard_document() refuses every mutation
# document - and that guarantee is not being weakened for this fix. The
# executor owns the two sends; everything that decides WHETHER to retry, WHAT
# the retry payload is, and WHAT reaches the audit trail lives here, where it
# is testable offline and shared by every future executor.
#
# The retry happens once. A second phone error on a payload that no longer
# carries a phone is not a phone problem, and looping on it would turn one bad
# record into an unbounded request stream.

TAG_PHONE_DROPPED = 'phone-dropped-invalid'

# Deliberately NOT the ImportLedger. That ledger forbids 'phone' outright
# (LEDGER_FORBIDDEN_KEYS) and always will. This file is the single, explicit,
# never-tracked exception: telling a merchant which number was lost requires
# the original string, and a hash cannot be handed back to a human to check.
# It carries real phone numbers, so it is gitignored for the same reason the
# manifest is.
DROPPED_PHONES_PATH = os.path.join(REPORTS_DIR, 'phase10_dropped_phones.jsonl')

DROPPED_PHONE_ALLOWED_KEYS = frozenset({
    'ts', 'run_id', 'woo_customer_id', 'operation', 'source', 'reason',
    'phone_original', 'phone_digit_count', 'user_errors', 'tag_applied',
    'customer_preserved',
})

# Why the phone was dropped. Distinguished because they mean different things
# upstream: INVALID is a data-quality defect in the source, TAKEN is a
# collision the phone-collision review should have caught and did not.
PHONE_DROP_INVALID = 'PHONE_INVALID'
PHONE_DROP_TAKEN = 'PHONE_ALREADY_TAKEN'
PHONE_DROP_OTHER = 'PHONE_ERROR_UNCLASSIFIED'

# Sources, so an offline pre-check flag is never mistaken for a live failure.
DROP_SOURCE_RETRY = 'RUNTIME_RETRY_AFTER_USER_ERROR'
DROP_SOURCE_PRECHECK = 'OFFLINE_FORMAT_PRECHECK'

_PHONE_FIELD_NAMES = frozenset({'phone'})
# Only consulted when the error carries no field at all. Anchored at the start
# so a message that merely mentions a phone number somewhere cannot match.
_PHONE_MESSAGE_RE = re.compile(r'^\s*phone\b', re.IGNORECASE)


def _error_field_names(error):
    """Field names in one userError.

    Shopify sends a list - ["phone"], sometimes ["input", "phone"] - but a
    bare string and an outright None both occur, so all three are handled
    rather than assumed away.
    """
    if not isinstance(error, dict):
        return []
    field = error.get('field')
    if field is None:
        return []
    if isinstance(field, str):
        return [field]
    return [str(f) for f in field]


def is_phone_user_error(user_errors):
    """True if any userError points at the phone field.

    Field-first: the field name is structured data Shopify controls, while the
    message is prose that changes between API versions. The message is only
    consulted when there is no field to read.
    """
    for error in user_errors or []:
        names = [n.lower() for n in _error_field_names(error)]
        if any(n in _PHONE_FIELD_NAMES for n in names):
            return True
        if not names and isinstance(error, dict):
            if _PHONE_MESSAGE_RE.match(str(error.get('message') or '')):
                return True
    return False


def classify_phone_error(user_errors):
    """Which kind of phone problem this is. See PHONE_DROP_* for why it matters."""
    for error in user_errors or []:
        if not isinstance(error, dict):
            continue
        names = [n.lower() for n in _error_field_names(error)]
        message = str(error.get('message') or '').lower()
        addressed = (any(n in _PHONE_FIELD_NAMES for n in names)
                     or (not names and _PHONE_MESSAGE_RE.match(message)))
        if not addressed:
            continue
        if 'taken' in message or 'already' in message or 'in use' in message:
            return PHONE_DROP_TAKEN
        if 'invalid' in message or 'not valid' in message:
            return PHONE_DROP_INVALID
        return PHONE_DROP_OTHER
    return PHONE_DROP_OTHER


class PhoneFallbackNotApplicable(ValueError):
    """The errors are not about the phone, or there is no phone left to drop.

    Raised rather than returning None because reaching here means a caller is
    retrying on an error this fallback cannot fix - a loop, not a save.
    """


def strip_phone_for_retry(customer_input, tag=TAG_PHONE_DROPPED):
    """A copy of the CustomerInput with the phone removed and the drop tagged.

    Copies rather than mutates: the original payload is what the ledger and the
    reconciliation template describe, and editing it in place would rewrite
    history to match the retry.

    The tag is how a dropped number stays visible in Shopify itself and not
    only in a local file - someone looking at the customer can see that a
    number was lost rather than never supplied.
    """
    if 'phone' not in customer_input:
        raise PhoneFallbackNotApplicable(
            'payload carries no phone field; a phone fallback cannot apply')
    retry = dict(customer_input)
    retry.pop('phone')
    tags = list(retry.get('tags') or [])
    if tag not in tags:
        tags.append(tag)
    retry['tags'] = tags
    # Copied, not shared: the retry carries its own metafield list so a later
    # edit to one payload cannot reach into the other.
    if 'metafields' in retry:
        retry['metafields'] = [dict(m) for m in retry['metafields']]
    # The point of the retry is that the customer survives WITH their identity
    # intact. A retry that saved the customer but lost the legacy id would be
    # worse than the failure it replaces.
    assert_legacy_metafield_present(retry)
    return retry


def assert_dropped_phone_record_safe(record):
    """Schema gate for the dropped-phone log. This file may carry a phone
    number - it may not quietly grow an email or an address as well."""
    unknown = set(record) - DROPPED_PHONE_ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f'dropped-phone record carries unapproved fields: {sorted(unknown)}')
    bad = (LEDGER_FORBIDDEN_KEYS - {'phone'}) & set(record)
    if bad:
        raise ValueError(f'dropped-phone record carries PII fields: {sorted(bad)}')
    return True


def append_dropped_phone(event, path=DROPPED_PHONES_PATH):
    """Append one event, flushed and fsynced. Same reasoning as ImportLedger:
    the records most worth keeping are the ones written just before a crash."""
    assert_dropped_phone_record_safe(event)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return event


def phone_fallback(customer_input, user_errors, woo_customer_id,
                   operation='customerCreate', run_id=None,
                   log_path=DROPPED_PHONES_PATH, now=None,
                   source=DROP_SOURCE_RETRY):
    """Decide and prepare the retry-without-phone. Sends nothing.

    Returns {'input': <retry payload>, 'event': <audit record>}. The caller
    re-issues its mutation with 'input' exactly once and records the outcome.

    Applies to customerCreate and customerUpdate alike - the field, the error
    shape and the correct response are identical, and only the operation name
    recorded in the audit trail differs.
    """
    if not is_phone_user_error(user_errors):
        raise PhoneFallbackNotApplicable(
            'userErrors do not name the phone field; this fallback does not apply')
    original = customer_input.get('phone')
    retry = strip_phone_for_retry(customer_input)
    stamp = (now or (lambda: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))()
    event = {
        'ts': stamp,
        'run_id': run_id,
        'woo_customer_id': int(woo_customer_id),
        'operation': operation,
        'source': source,
        'reason': classify_phone_error(user_errors),
        'phone_original': original,
        'phone_digit_count': len(phone_digits(original)),
        'user_errors': sanitize_user_errors(user_errors),
        'tag_applied': TAG_PHONE_DROPPED,
        'customer_preserved': True,
    }
    if log_path:
        append_dropped_phone(event, log_path)
    return {'input': retry, 'event': event}


def offline_phone_flag_event(woo_customer_id, phone, reason, run_id=None,
                            now=None, operation='OFFLINE_PRECHECK'):
    """A dropped-phone record for a number the OFFLINE pre-check flagged.

    Same file and same schema as a live retry, distinguished by `source` - one
    log of every number this migration expects to lose or did lose, rather than
    two that have to be joined by hand. `customer_preserved` is True because
    the runtime fallback is what makes that true; a flag here is a warning, not
    an exclusion, and this function has no power to drop anyone.
    """
    stamp = (now or (lambda: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))()
    return {
        'ts': stamp,
        'run_id': run_id,
        'woo_customer_id': int(woo_customer_id),
        'operation': operation,
        'source': DROP_SOURCE_PRECHECK,
        'reason': reason,
        'phone_original': phone,
        'phone_digit_count': len(phone_digits(phone)),
        'user_errors': [],
        'tag_applied': None,
        'customer_preserved': True,
    }


# --------------------------------------------------------------------------
# Offline phone format pre-check  (risk #45, second half)
# --------------------------------------------------------------------------
#
# The retry above stops a bad phone costing a customer. It does not tell anyone
# HOW MANY bad phones there are, and "we will find out during the import" is not
# a plan for 4,450 numbers. This classifies them beforehand, offline, from the
# source dump - no request, no store, no credentials.
#
# WHAT "VALID" HERE DOES AND DOES NOT MEAN
# ----------------------------------------
# This is a STRUCTURAL check: characters, the plus sign, and digit count. It
# cannot promise Shopify will accept a number, because Shopify validates
# against national numbering plans that this project has no copy of. Treating
# a green result here as a guarantee would recreate exactly the false
# confidence that lost woo_customer_id=1 - which, note, would have passed a
# pure length check: 11 digits, correctly plus-prefixed, and rejected anyway
# because +44 7... needs a 10-digit national number and it carried 9.
#
# So the two halves of this fix are not alternatives. The pre-check sizes the
# problem; the retry is what actually makes it survivable.

PHONE_FORMAT_VALID = 'VALID_E164'
PHONE_FORMAT_NEEDS_NORMALIZATION = 'NEEDS_NORMALIZATION'
PHONE_FORMAT_INVALID = 'INVALID_FORMAT'
PHONE_FORMAT_ABSENT = 'NO_PHONE'

# The floor is this project's rule, not ITU's - E.164 permits shorter national
# numbers in some plans. 15 is the real E.164 ceiling.
PHONE_MIN_DIGITS = 10
PHONE_MAX_DIGITS = 15

# Separators people actually type. Anything else is a data-entry accident
# (a name, an extension marker, two numbers in one field) and is not silently
# stripped - a number that has to be repaired to be sent is not a number this
# migration should be sending.
_PHONE_ALLOWED_CHARS_RE = re.compile(r'^[0-9 ()+.\-/]+$')

REASON_EMPTY = 'EMPTY'
REASON_ILLEGAL_CHARS = 'ILLEGAL_CHARACTERS'
REASON_PLUS_MISPLACED = 'PLUS_NOT_LEADING_OR_REPEATED'
REASON_TOO_FEW = 'TOO_FEW_DIGITS'
REASON_TOO_MANY = 'TOO_MANY_DIGITS'
REASON_CC_LEADING_ZERO = 'COUNTRY_CODE_STARTS_WITH_ZERO'
REASON_E164 = 'PLUS_PREFIXED_INTERNATIONAL'
REASON_INTL_PREFIX_00 = 'INTERNATIONAL_PREFIX_00_NOT_PLUS'
REASON_NATIONAL_LEADING_ZERO = 'NATIONAL_FORMAT_LEADING_ZERO'
REASON_NO_COUNTRY_CODE = 'NO_COUNTRY_CODE_PREFIX'


def classify_phone_format(raw):
    """(category, reason) for one source phone string. Pure, offline, no I/O.

    Never rewrites the value. Normalisation is Shopify's job at write time -
    empirically it did exactly that in the Gate 6 run, turning 07... into
    +44... - so NEEDS_NORMALIZATION is a description of the input, not a
    prediction of failure.
    """
    value = (raw or '').strip()
    if not value:
        return PHONE_FORMAT_ABSENT, REASON_EMPTY
    if not _PHONE_ALLOWED_CHARS_RE.match(value):
        return PHONE_FORMAT_INVALID, REASON_ILLEGAL_CHARS
    if value.count('+') > 1 or ('+' in value and not value.startswith('+')):
        return PHONE_FORMAT_INVALID, REASON_PLUS_MISPLACED

    digits = phone_digits(value)
    if len(digits) < PHONE_MIN_DIGITS:
        return PHONE_FORMAT_INVALID, REASON_TOO_FEW
    if len(digits) > PHONE_MAX_DIGITS:
        return PHONE_FORMAT_INVALID, REASON_TOO_MANY

    if value.startswith('+'):
        # No country code begins with 0, so "+0..." is a malformed number
        # rather than an unusual one.
        if digits.startswith('0'):
            return PHONE_FORMAT_INVALID, REASON_CC_LEADING_ZERO
        return PHONE_FORMAT_VALID, REASON_E164
    if digits.startswith('00'):
        return PHONE_FORMAT_NEEDS_NORMALIZATION, REASON_INTL_PREFIX_00
    if digits.startswith('0'):
        return PHONE_FORMAT_NEEDS_NORMALIZATION, REASON_NATIONAL_LEADING_ZERO
    return PHONE_FORMAT_NEEDS_NORMALIZATION, REASON_NO_COUNTRY_CODE


# Advisory only, and deliberately narrow: the national significant number
# length for the one country plan this dataset is dominated by. GB mobiles are
# 7 followed by nine more digits; everything else GB is nine or ten. This is
# the rule that catches the woo_customer_id=1 shape, which every generic check
# passes.
#
# It is reported separately from the three categories above rather than folded
# into INVALID_FORMAT, because it rests on a numbering plan held in this file
# rather than on a structural property of the string - and a plan copied into
# source is a thing that goes stale.
GB_MOBILE_NSN_DIGITS = 10
GB_OTHER_NSN_DIGITS = (9, 10)

ADVISORY_GB_NSN_LENGTH = 'GB_NATIONAL_NUMBER_WRONG_LENGTH'


def phone_plan_advisory(raw):
    """A country-plan concern, or None. Never changes a category by itself."""
    value = (raw or '').strip()
    digits = phone_digits(value)
    if not digits:
        return None
    if value.startswith('+') and digits.startswith('44'):
        nsn = digits[2:]
    elif digits.startswith('0') and len(digits) in (10, 11):
        nsn = digits[1:]          # GB national format, 0 + NSN
    else:
        return None
    if nsn.startswith('7'):
        return None if len(nsn) == GB_MOBILE_NSN_DIGITS else ADVISORY_GB_NSN_LENGTH
    return None if len(nsn) in GB_OTHER_NSN_DIGITS else ADVISORY_GB_NSN_LENGTH


def phone_format_summary(candidates):
    """Aggregate counts only - safe to write to a tracked report.

    Returns categories, reason breakdown, a digit-length histogram, and the
    advisory count. No phone number and no customer identifier is included;
    the caller decides what per-record detail it needs and where that goes.
    """
    categories, reasons, lengths = {}, {}, {}
    advisory = 0
    for cand in candidates:
        raw = cand.get('phone')
        category, reason = classify_phone_format(raw)
        categories[category] = categories.get(category, 0) + 1
        if category != PHONE_FORMAT_ABSENT:
            reasons[reason] = reasons.get(reason, 0) + 1
            n = len(phone_digits(raw))
            lengths[n] = lengths.get(n, 0) + 1
            if phone_plan_advisory(raw):
                advisory += 1
    return {
        'customers_scanned': len(candidates),
        'categories': categories,
        'reasons': reasons,
        'digit_length_histogram': {str(k): lengths[k] for k in sorted(lengths)},
        'gb_plan_advisory_count': advisory,
    }


# --------------------------------------------------------------------------
# Operation planning - proves ordering without executing anything
# --------------------------------------------------------------------------

STAGE_CUSTOMER = 'customerCreate'
STAGE_ADDRESS = 'customerAddressCreate'


class NotEligibleForImport(ValueError):
    """A record that must never reach customerCreate."""


def _is_valid_email(email):
    """Local mirror of the dry run's email test, kept here so this module has
    no import cycle with phase10_customer_dry_run."""
    return bool(email) and bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email))


def plan_customer_import(candidate, include_shipping=False, phone_allowed=True,
                         include_registered_at=True, country_codes=None,
                         address_policy=None):
    """Ordered stage plan for one customer. Builds inputs; executes nothing.

    Stage 1 is always customerCreate, carrying the legacy metafield inline.
    Stage 2 onwards are customerAddressCreate calls, which cannot even be
    addressed without a customerId that does not exist until stage 1 returns -
    the ordering is a structural constraint here, not a convention.

    Raises NotEligibleForImport for anything the dry run would have
    quarantined, so the 292 missing-email records cannot reach customerCreate
    through this path.
    """
    email = (candidate.get('email') or '').strip()
    if not email:
        raise NotEligibleForImport(
            'woo_customer_id=%s has no email; quarantined records never reach '
            'customerCreate' % candidate.get('woo_customer_id'))
    if not _is_valid_email(candidate.get('email_raw') or email):
        raise NotEligibleForImport(
            'woo_customer_id=%s has an invalid email' % candidate.get('woo_customer_id'))

    stages = [{
        'stage': STAGE_CUSTOMER,
        'input': build_customer_input(candidate,
                                      include_registered_at=include_registered_at,
                                      phone_allowed=phone_allowed),
        'requires_customer_id': False,
    }]
    for planned in plan_addresses(candidate, include_shipping, country_codes,
                                  policy=address_policy):
        stages.append({
            'stage': STAGE_ADDRESS,
            'address': planned['address'],
            'setAsDefault': planned['setAsDefault'],
            'kind': planned['kind'],
            'flags': planned['flags'],
            'requires_customer_id': True,
        })
    return stages


# --------------------------------------------------------------------------
# Phone ownership evidence
# --------------------------------------------------------------------------
#
# Approved rule (2026-08-21): keep the number only on the customer for whom
# there is the strongest evidence it is genuinely their individual number. If
# it looks shared, business, or placeholder, omit it from Shopify entirely
# rather than inventing an owner or trying to make Shopify accept duplicates.
#
# This module SCORES that evidence and RECOMMENDS. It never edits a phone
# number, never deletes one from the source, and never writes to Shopify. The
# reviewer_decision column is what actually decides; the recommendation exists
# so a reviewer starts from evidence rather than a blank sheet.

# Signal weights. Positive = evidence the number is this individual's own.
# Negative = evidence the number belongs to an organisation rather than a person.
EVIDENCE_REGISTERED_ACCOUNT = 2      # a real WP account, not a one-off guest checkout
EVIDENCE_PHONE_ON_OWN_PROFILE = 3    # the number is on THEIR profile, not scraped
                                     # from an order that could have been placed
                                     # by anyone at that address
EVIDENCE_COMPANY_PRESENT = -2        # a company on the record makes a business
                                     # switchboard the likelier explanation

# A member must clear this to be called an owner at all. Set so that a guest
# row with a profile phone (3) or a registered account with one (5) qualifies,
# but a bare registered account (2) does not - an account alone says nothing
# about whose phone it is.
MIN_OWNERSHIP_SCORE = 3


def phone_evidence(member):
    """(score, signals) for one member of a collision group.

    member needs: is_registered, phone_from_profile, company.
    phone_from_profile must be set by the caller from wp_usermeta - it is the
    single most useful signal and is NOT derivable from the candidate dict
    alone, because build_candidate() silently falls back to an order's billing
    phone when the customer has no address of their own.
    """
    score = 0
    signals = []
    if member.get('is_registered'):
        score += EVIDENCE_REGISTERED_ACCOUNT
        signals.append('registered_account')
    else:
        signals.append('guest_row')
    if member.get('phone_from_profile'):
        score += EVIDENCE_PHONE_ON_OWN_PROFILE
        signals.append('phone_on_own_profile')
    else:
        signals.append('phone_from_order_fallback')
    if (member.get('company') or '').strip():
        score += EVIDENCE_COMPANY_PRESENT
        signals.append('company_present_suggests_business')
    return score, signals


def recommend_group_action(members, high_risk_size=None):
    """(action, owner_woo_id_or_None, rationale) for one collision group.

    Deterministic and explainable - a reviewer must be able to see why, and
    reach a different conclusion if they know something the data does not.

      OMIT_FROM_ALL          the group is too large to be a household line, or
                             no member has evidence the number is personally
                             theirs. "We cannot tell whose this is" resolves to
                             "send it for nobody", never "send it for the first
                             row we happened to read".
      MANUAL_REVIEW_REQUIRED two or more members each have genuine individual
                             evidence. A real person has to choose; guessing
                             here would assign one person's phone to a record
                             that is arguably someone else's.
      KEEP_ONE               exactly one member clears the threshold and is
                             strictly ahead of everyone else.
    """
    high_risk_size = HIGH_RISK_GROUP_SIZE if high_risk_size is None else high_risk_size
    size = len(members)

    if size >= high_risk_size:
        return (ACTION_OMIT_FROM_ALL, None,
                '%d customers share this number - far beyond a household line, so it '
                'reads as a shop, switchboard, or form default. Omitted from all.' % size)

    scored = []
    for m in members:
        score, signals = phone_evidence(m)
        scored.append((score, m['woo_customer_id'], m, signals))
    scored.sort(key=lambda t: (-t[0], t[1]))

    qualified = [t for t in scored if t[0] >= MIN_OWNERSHIP_SCORE]
    if not qualified:
        return (ACTION_OMIT_FROM_ALL, None,
                'no member reaches the ownership threshold (best score %d of %d needed) - '
                'nothing distinguishes an individual owner, so treated as shared.'
                % (scored[0][0], MIN_OWNERSHIP_SCORE))

    top_score = qualified[0][0]
    leaders = [t for t in qualified if t[0] == top_score]
    if len(leaders) > 1:
        return (ACTION_MANUAL_REVIEW, None,
                '%d members tie on the strongest evidence (score %d each: %s) - a human '
                'must choose, since each has a genuine claim.'
                % (len(leaders), top_score,
                   ', '.join(str(t[1]) for t in leaders)))

    winner = qualified[0]
    return (ACTION_KEEP_ONE, winner[1],
            'woo_customer_id=%d scores %d (%s), strictly ahead of the next best (%d).'
            % (winner[1], winner[0], '+'.join(winner[3]), scored[1][0]))


SEND_PHONE = 'SEND_PHONE'
OMIT_PHONE = 'OMIT_PHONE'
HOLD_PENDING_REVIEW = 'OMIT_PHONE_PENDING_REVIEW'


def final_phone_action(woo_id, action, owner_id, reviewer_decision=None,
                       reviewer_owner=None):
    """What the importer would actually do for one customer.

    A reviewer decision always overrides the recommendation. An unresolved
    group holds at OMIT - the customer is still created, just without a phone.
    No path here results in two customers being sent the same number.
    """
    effective_action = reviewer_decision or action
    effective_owner = reviewer_owner if reviewer_decision else owner_id

    if effective_action == ACTION_KEEP_ONE:
        return SEND_PHONE if int(woo_id) == int(effective_owner) else OMIT_PHONE
    if effective_action == ACTION_OMIT_FROM_ALL:
        return OMIT_PHONE
    return HOLD_PENDING_REVIEW


# --------------------------------------------------------------------------
# Conflicting-identity resolution
# --------------------------------------------------------------------------
#
# 247 quarantined rows share an email with an already-included customer but
# carry a different name. Nothing in WooCommerce establishes which name is
# correct - there is no ordering, no authority, no last-updated marker between
# the two variants. So nothing here guesses. The reviewer writes a token, this
# code resolves the token to a name, and an unresolved row stays unresolved.
#
# A typo'd or unrecognised token raises rather than falling back to a default.
# Silently treating "IMPORT" or "import_name " as IMPORT_NAME would be exactly
# the kind of helpful guess that puts the wrong name on a real person.

CHOICE_IMPORT_NAME = 'IMPORT_NAME'
CHOICE_ALTERNATE_NAME = 'ALTERNATE_NAME'
CHOICE_MANUAL_REVIEW = 'MANUAL_REVIEW'
VALID_NAME_CHOICES = (CHOICE_IMPORT_NAME, CHOICE_ALTERNATE_NAME, CHOICE_MANUAL_REVIEW)

NAME_REVIEW_PENDING = 'PENDING'
NAME_REVIEW_RESOLVED = 'RESOLVED'


class UnrecognisedNameChoice(ValueError):
    """The reviewer wrote something that is not one of the three tokens."""


def resolve_chosen_name(row):
    """The confirmed name for one conflict row, or None if unresolved.

    row needs: chosen_name (the reviewer's token), import_name, alternate_name.

    Returns None for MANUAL_REVIEW and for an empty cell - both mean "no human
    has confirmed this yet", and they are deliberately not distinguished here:
    an unreviewed row and a row a reviewer looked at and could not decide are
    equally unconfirmed as far as the importer is concerned.
    """
    choice = (row.get('chosen_name') or '').strip()
    if not choice or choice == CHOICE_MANUAL_REVIEW:
        return None
    if choice not in VALID_NAME_CHOICES:
        raise UnrecognisedNameChoice(
            'conflict row for woo_customer_id=%s has chosen_name=%r, which is not one '
            'of %s. Refusing to interpret it - fix the cell rather than letting this '
            'be guessed at.' % (row.get('woo_customer_id'), choice,
                                ', '.join(VALID_NAME_CHOICES)))
    if choice == CHOICE_IMPORT_NAME:
        return row.get('import_name')
    return row.get('alternate_name')


def unresolved_name_conflicts(review_rows):
    """Woo customer ids whose name is not yet confirmed by a human."""
    return sorted(int(r['woo_customer_id']) for r in review_rows
                  if resolve_chosen_name(r) is None)


# Policy for what an unresolved conflict does to the run.
#
# The choice matters more than it first looks. Every one of the 247 conflicting
# emails ALREADY has an IMPORT row, so the customer is created either way - the
# question is whether they are created carrying a name nobody has confirmed.
POLICY_BLOCK_ALL = 'BLOCK_ENTIRE_MIGRATION'
POLICY_EXCLUDE_AFFECTED = 'EXCLUDE_AFFECTED_CUSTOMERS'
POLICY_PROCEED_WITH_IMPORT_NAME = 'PROCEED_WITH_IMPORT_NAME'

NAME_CONFLICT_POLICIES = (POLICY_BLOCK_ALL, POLICY_EXCLUDE_AFFECTED,
                          POLICY_PROCEED_WITH_IMPORT_NAME)


class NameConflictsUnresolved(RuntimeError):
    """Raised under POLICY_BLOCK_ALL when any conflict is still unconfirmed."""


def name_conflict_gate(review_rows, manifest_woo_ids, policy=POLICY_EXCLUDE_AFFECTED):
    """Apply the conflicting-identity policy to a planned import population.

    Returns {'policy', 'unresolved', 'resolved', 'excluded', 'proceed_with',
             'name_overrides'}.

      proceed_with   - the Woo ids the importer may create under this policy
      excluded       - ids held back
      name_overrides - {woo_id: confirmed_name} for rows a reviewer HAS decided,
                       applied whichever policy is in force

    POLICY_BLOCK_ALL raises rather than returning, because a policy that says
    "do not proceed" should not hand back a population that could be imported
    by a caller who ignores the report.
    """
    if policy not in NAME_CONFLICT_POLICIES:
        raise ValueError('unknown name-conflict policy %r; expected one of %s'
                         % (policy, ', '.join(NAME_CONFLICT_POLICIES)))

    unresolved, overrides = [], {}
    for row in review_rows:
        woo_id = int(row['woo_customer_id'])
        confirmed = resolve_chosen_name(row)
        if confirmed is None:
            unresolved.append(woo_id)
        else:
            overrides[woo_id] = confirmed

    unresolved_set = set(unresolved)
    manifest = [int(i) for i in manifest_woo_ids]

    if policy == POLICY_BLOCK_ALL and unresolved_set:
        raise NameConflictsUnresolved(
            '%d conflicting identity/identities are unconfirmed; policy %s forbids '
            'starting the migration. Resolve them or change the policy deliberately.'
            % (len(unresolved_set), POLICY_BLOCK_ALL))

    if policy == POLICY_EXCLUDE_AFFECTED:
        proceed = [i for i in manifest if i not in unresolved_set]
        excluded = [i for i in manifest if i in unresolved_set]
    else:
        proceed = list(manifest)
        excluded = []

    return {
        'policy': policy,
        'unresolved': sorted(unresolved_set),
        'resolved': sorted(overrides),
        'excluded': excluded,
        'proceed_with': proceed,
        'name_overrides': overrides,
    }


def apply_name_override(candidate, name_overrides):
    """Return a COPY of the candidate with a reviewer-confirmed name applied.

    Copies rather than mutating: the source candidate is evidence, and an
    importer that quietly rewrote it in place would make the audit trail lie
    about what the source said.
    """
    woo_id = int(candidate['woo_customer_id'])
    if woo_id not in name_overrides:
        return candidate
    confirmed = (name_overrides[woo_id] or '').strip()
    updated = dict(candidate)
    parts = confirmed.split(' ', 1)
    updated['first_name'] = parts[0] if parts and parts[0] else ''
    updated['last_name'] = parts[1] if len(parts) > 1 else ''
    return updated


# --------------------------------------------------------------------------
# Address outcome vocabulary
# --------------------------------------------------------------------------
#
# Ratified 2026-08-21: an address whose country cannot be established is
# SKIPPED. The customer is unaffected and still imports. GB is never assumed -
# a default country would silently invent a delivery destination, and a wrong
# address is worse than no address.
#
#     Customer = IMPORT
#     Address  = SKIPPED_INVALID_COUNTRY
#
# The status is the OUTCOME (what happened to the address). The flag alongside
# it is the REASON (why). Three distinct source problems collapse to the same
# outcome but stay distinguishable in the reason, because "no country recorded"
# and "country recorded as free text we cannot map" need different fixes from
# whoever cleans the source data.

ADDRESS_STATUS_PLANNED = 'ADDRESS_PLANNED'
ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY = 'SKIPPED_INVALID_COUNTRY'
ADDRESS_STATUS_SKIPPED_NO_STREET = 'SKIPPED_NO_ADDRESS1'
ADDRESS_STATUS_NO_SOURCE_ADDRESS = 'NO_ADDRESS_IN_SOURCE'

# Every reason that resolves to SKIPPED_INVALID_COUNTRY.
COUNTRY_SKIP_REASONS = frozenset({
    ADDRESS_SKIP_NO_COUNTRY,        # no country recorded at all
    ADDRESS_SKIP_BAD_COUNTRY,       # recorded, but not a CountryCode enum value
    ADDRESS_SKIP_UNKNOWN_REGION,    # recorded as ZZ, "Unknown Region"
})


def address_status(address, flags):
    """(status, reason) for one attempted address.

    A skipped address NEVER affects the customer. Nothing in this function or
    its callers can turn an address problem into a customer problem - that
    separation is the whole point of the ratified rule, and
    plan_customer_import() enforces it by always emitting the customerCreate
    stage first, independently of any address outcome.
    """
    flags = list(flags or [])
    if address is not None:
        return ADDRESS_STATUS_PLANNED, '; '.join(flags)
    reasons = set(flags)
    if reasons & COUNTRY_SKIP_REASONS:
        return (ADDRESS_STATUS_SKIPPED_INVALID_COUNTRY,
                '; '.join(sorted(reasons & COUNTRY_SKIP_REASONS)))
    if ADDRESS_SKIP_NO_STREET in reasons:
        return ADDRESS_STATUS_SKIPPED_NO_STREET, ADDRESS_SKIP_NO_STREET
    return ADDRESS_STATUS_NO_SOURCE_ADDRESS, '; '.join(flags)


def customer_remains_importable(_address_status):
    """Always True. Present as an explicit, testable statement rather than an
    implicit property of the code, because 'the customer still imports' is the
    part of this decision that must never quietly stop being true."""
    return True


def describe_address_outcome(candidate, kind='billing', country_codes=None):
    """(status, reason, address_or_None) for one customer and address kind."""
    address, flags = build_address_input(candidate, kind, country_codes)
    status, reason = address_status(address, flags)
    return status, reason, address


# --------------------------------------------------------------------------
# Customer metafield contract (ratified 2026-08-21)
# --------------------------------------------------------------------------
#
# custom.legacy_woo_customer_id is MANDATORY on every created customer. It is
# not a default that can be turned off, and build_customer_input() has no
# parameter to omit it - because everything downstream depends on it existing:
#
#     Woo ID -> custom.legacy_woo_customer_id -> Shopify Customer GID
#
# That chain is what makes the import resumable, reconcilable, and reversible.
# A customer created without it is unidentifiable: not matchable back to
# WooCommerce, not skippable on a re-run, not findable for rollback. One such
# customer silently breaks every one of those guarantees, so the invariant is
# asserted on the payload rather than trusted to remain true.
#
# custom.woo_registered_at is RETAINED. Shopify's Customer.createdAt is
# server-controlled and read-only; it is never written, manipulated, or
# approximated. The registration date lives in a metafield or nowhere.

REGISTERED_AT_KEY = 'woo_registered_at'
REGISTERED_AT_TYPE = 'single_line_text_field'

# Shopify-owned fields no payload may ever carry. createdAt is listed because
# "set the created date to match WooCommerce" is a request that sounds
# reasonable and cannot be honoured - the API ignores or rejects it, and code
# that appears to try implies to a reader that it works.
SERVER_CONTROLLED_CUSTOMER_FIELDS = frozenset({
    'createdAt', 'created_at', 'updatedAt', 'updated_at', 'id', 'legacyResourceId',
})


class LegacyMetafieldMissing(RuntimeError):
    """A customer payload reached a checkpoint without its legacy id."""


def assert_legacy_metafield_present(customer_input):
    """Raise unless the payload carries a well-formed legacy id metafield.

    Call before any create. The failure mode this prevents - a customer created
    without a legacy id - is silent, permanent, and only discovered during
    reconciliation, by which point the customer exists and cannot be identified
    to fix.
    """
    metafields = customer_input.get('metafields') or []
    for mf in metafields:
        if mf.get('namespace') == LEGACY_NAMESPACE and mf.get('key') == LEGACY_KEY:
            value = str(mf.get('value') or '').strip()
            if not value:
                raise LegacyMetafieldMissing(
                    'legacy id metafield is present but empty - a blank identifier is '
                    'no better than a missing one')
            if mf.get('type') != LEGACY_TYPE:
                raise LegacyMetafieldMissing(
                    'legacy id metafield has type %r, expected %r - the product '
                    'precedent (596 live products) uses %s and a mismatched type '
                    'breaks value comparison on lookup'
                    % (mf.get('type'), LEGACY_TYPE, LEGACY_TYPE))
            return True
    raise LegacyMetafieldMissing(
        'customer payload carries no %s.%s metafield. Every created customer must '
        'have one: it is the only link back to WooCommerce, and without it the '
        'customer cannot be reconciled, resumed past, or rolled back.'
        % (LEGACY_NAMESPACE, LEGACY_KEY))


def assert_no_server_controlled_fields(customer_input):
    """Raise if a payload tries to set a field Shopify owns."""
    present = SERVER_CONTROLLED_CUSTOMER_FIELDS & set(customer_input)
    if present:
        raise ValueError(
            'customer payload sets server-controlled field(s) %s. Shopify assigns '
            'these; Customer.createdAt in particular cannot be back-dated to the '
            'WooCommerce registration date - that is what custom.%s is for.'
            % (sorted(present), REGISTERED_AT_KEY))
    return True


def legacy_metafield(woo_customer_id):
    """The mandatory identity metafield for one customer."""
    return {
        'namespace': LEGACY_NAMESPACE,
        'key': LEGACY_KEY,
        'type': LEGACY_TYPE,
        'value': str(woo_customer_id),
    }


def registered_at_metafield(date_registered):
    """The retained registration-date metafield, or None if the source has no
    date. Never invented: a guest checkout row has no registration event, and a
    fabricated date would be indistinguishable from a real one."""
    value = str(date_registered or '').strip()
    if not value:
        return None
    return {
        'namespace': LEGACY_NAMESPACE,
        'key': REGISTERED_AT_KEY,
        'type': REGISTERED_AT_TYPE,
        'value': value,
    }


# --------------------------------------------------------------------------
# Province code validation (ratified 2026-08-21)
# --------------------------------------------------------------------------
#
#     GB                      -> omit provinceCode (ratified)
#     country with provinces  -> validate against Shopify's accepted codes
#                                  valid   -> send
#                                  invalid -> omit + flag
#     country without         -> omit + flag
#
# This replaces PROVINCE_CODE_COUNTRIES, a hand-written allowlist of country
# codes that gated on the COUNTRY and never checked the VALUE. It was wrong in
# both directions: it excluded Ireland, whose 26 counties Shopify does accept,
# and it would have sent whatever string a listed country happened to carry.
#
# The accepted codes come from Shopify's own region data - see
# migration/schema/shopify_province_codes.json and the fetch script beside it.

# Province validation lives in phase10_province_validator - the canonical home.
# Imported rather than duplicated: two implementations of this rule would drift
# silently, one path sending a code the other omits, with nothing to catch it.
# Re-exported here so existing callers and tests keep working unchanged.
from phase10_province_validator import (  # noqa: E402
    PROVINCE_CODES_BY_COUNTRY,
    COUNTRIES_WITHOUT_PROVINCES,
    PROVINCE_OMIT_COUNTRIES,
    PROVINCE_SENT,
    PROVINCE_DROPPED_GB_RULE,
    PROVINCE_DROPPED_COUNTRY_HAS_NONE,
    PROVINCE_DROPPED_INVALID,
    PROVINCE_DROPPED_COUNTRY_UNKNOWN,
    AUDITABLE_FLAGS,
    ProvinceFlagLedger,
    accepted_codes_for,
    assert_no_raw_text_in_province,
    load_province_codes,
    province_code_is_valid,
    validate_province_code,
)
