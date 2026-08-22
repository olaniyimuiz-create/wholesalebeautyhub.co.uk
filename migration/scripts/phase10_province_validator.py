"""Phase 10 province code validation. OFFLINE ONLY - never contacts Shopify.

CANONICAL HOME for province validation. phase10_import_runtime imports from
here rather than keeping its own copy: two implementations of a rule this
specific will drift, and the drift would be silent - one path sending a code
the other omits, with nothing to catch it.

RULES
-----
    GB                        -> ALWAYS omit provinceCode.
                                 Never "Surrey", never "West Midlands", and
                                 never a genuine GB zone either.
    non-GB, valid ISO code    -> pass as provinceCode on the address input
    non-GB, invalid/arbitrary -> OMIT and write an audit flag
    country with no provinces -> OMIT and write an audit flag

Arbitrary, unvalidated text is NEVER passed into provinceCode under any
circumstance. There is no code path that does it, and a test asserts as much.

LOOKUP TABLE
------------
migration/schema/shopify_province_codes.json - 756 codes across 22 countries,
taken from Shopify's own published region data (github.com/Shopify/worldwide,
data/regions/<CC>.yml). Shopify's province codes follow ISO 3166-2, but the
accepted SET per country is Shopify's to define, so it is taken from Shopify
rather than from a generic ISO table.

Live cross-check against this store is an ACCEPTED LIMITATION (risk register
#43): the app lacks read_shipping/read_markets. That is a gap in independent
confirmation, NOT evidence the table is wrong, and no scope change is being
requested.
"""
import json
import os

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schema',
    'shopify_province_codes.json')

FLAGS_PATH = os.path.join('reports', 'phase10_province_validation_flags.jsonl')

# Ratified 2026-08-21, mandatory. Shopify DOES define GB zones (ENG, NIR, SCT,
# WLS, BFP), so this is a deliberate override rather than an absence: WooCommerce
# holds counties in billing_state, which are not those zones and never will be.
PROVINCE_OMIT_COUNTRIES = frozenset({'GB'})

# Outcome codes. The status says what happened; the flag says why.
PROVINCE_SENT = 'PROVINCE_CODE_SENT'
PROVINCE_DROPPED_GB_RULE = 'PROVINCE_DROPPED_GB_RATIFIED_RULE'
PROVINCE_DROPPED_COUNTRY_HAS_NONE = 'PROVINCE_DROPPED_COUNTRY_HAS_NO_PROVINCES'
PROVINCE_DROPPED_INVALID = 'PROVINCE_DROPPED_INVALID_CODE'
PROVINCE_DROPPED_COUNTRY_UNKNOWN = 'PROVINCE_DROPPED_COUNTRY_NOT_IN_DATASET'

# Flags that mean "a human may want to look at the source data". The GB rule is
# deliberately NOT one of them: 2,483 GB records dropping a county is the
# designed behaviour, not an anomaly, and flagging them would bury the 20 that
# actually warrant attention.
AUDITABLE_FLAGS = frozenset({
    PROVINCE_DROPPED_INVALID,
    PROVINCE_DROPPED_COUNTRY_HAS_NONE,
    PROVINCE_DROPPED_COUNTRY_UNKNOWN,
})


def load_province_codes(path=SCHEMA_PATH):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - table is optional for pure-transform use
        return {'provinces': {}, '_countries_with_no_zones': []}


_DATA = load_province_codes()
PROVINCE_CODES_BY_COUNTRY = _DATA.get('provinces') or {}
COUNTRIES_WITHOUT_PROVINCES = frozenset(_DATA.get('_countries_with_no_zones') or [])


def validate_province_code(country_code, province_value):
    """(code_to_send_or_None, flag).

    Exact match after strip and upper-case. No fuzzy matching, no name-to-code
    resolution, no stripping of an ISO country prefix: a value that is not a
    code Shopify accepts is omitted and flagged for a human, never coerced into
    one that happens to be close. "California" does not become "CA" here.

    An omitted provinceCode never blocks the address, and an address problem
    never blocks the customer.
    """
    country = (country_code or '').strip().upper()
    value = (province_value or '').strip()
    if not value:
        return None, None

    if country in PROVINCE_OMIT_COUNTRIES:
        return None, PROVINCE_DROPPED_GB_RULE
    if country in COUNTRIES_WITHOUT_PROVINCES:
        return None, PROVINCE_DROPPED_COUNTRY_HAS_NONE
    accepted = PROVINCE_CODES_BY_COUNTRY.get(country)
    if not accepted:
        return None, PROVINCE_DROPPED_COUNTRY_UNKNOWN
    if value.upper() in accepted:
        return value.upper(), PROVINCE_SENT
    return None, PROVINCE_DROPPED_INVALID


def province_code_is_valid(country_code, province_value):
    code, _flag = validate_province_code(country_code, province_value)
    return code is not None


def accepted_codes_for(country_code):
    """The ISO province codes Shopify accepts for one country. Empty means the
    country has none - which is a real answer, not a lookup failure."""
    return dict(PROVINCE_CODES_BY_COUNTRY.get((country_code or '').strip().upper(), {}))


class ProvinceFlagLedger:
    """Append-only audit ledger for province values that could not be sent.

    GITIGNORED. A province value plus a Woo customer id is an address component
    tied to an identifier, so it is treated exactly like the address exception
    report: per-record detail stays untracked, aggregates are safe to commit.
    """

    def __init__(self, path=FLAGS_PATH):
        self.path = path
        self.counts = {}
        self._opened = False

    def _open(self):
        if self._opened:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Truncate once per run so the ledger reflects this run, not an
        # accumulation across runs that would double-count on re-execution.
        open(self.path, 'w', encoding='utf-8').close()
        self._opened = True

    def flag(self, woo_customer_id, address_kind, country_code, raw_value, flag):
        self.counts[flag] = self.counts.get(flag, 0) + 1
        if flag not in AUDITABLE_FLAGS:
            return None
        self._open()
        entry = {
            'woo_customer_id': int(woo_customer_id),
            'address_kind': address_kind,
            'country_code': (country_code or '').strip().upper(),
            'raw_province_value': raw_value,
            'flag': flag,
            'action_taken': 'provinceCode OMITTED from the payload',
            'recommended_human_action': (
                'Correct the state/province in WooCommerce to a code Shopify accepts, '
                'or accept the address importing without a province.'),
        }
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, sort_keys=True) + '\n')
            f.flush()
            os.fsync(f.fileno())
        return entry

    def summary(self):
        """Aggregate counts only - safe for a tracked report."""
        return {
            'by_flag': dict(sorted(self.counts.items())),
            'sent': self.counts.get(PROVINCE_SENT, 0),
            'omitted_gb_rule': self.counts.get(PROVINCE_DROPPED_GB_RULE, 0),
            'omitted_invalid_code': self.counts.get(PROVINCE_DROPPED_INVALID, 0),
            'omitted_country_has_no_provinces': self.counts.get(
                PROVINCE_DROPPED_COUNTRY_HAS_NONE, 0),
            'omitted_country_unknown': self.counts.get(
                PROVINCE_DROPPED_COUNTRY_UNKNOWN, 0),
            'auditable_flags_written': sum(
                c for f, c in self.counts.items() if f in AUDITABLE_FLAGS),
        }


def assert_no_raw_text_in_province(address_input, country_code):
    """Raise if an address input carries a provinceCode that is not an accepted
    code for its country. The last line of defence before a payload is sent."""
    code = (address_input or {}).get('provinceCode')
    if code is None:
        return True
    country = (country_code or '').strip().upper()
    if country in PROVINCE_OMIT_COUNTRIES:
        raise ValueError(
            f'address for {country} carries provinceCode={code!r}; GB must always '
            f'omit it')
    accepted = PROVINCE_CODES_BY_COUNTRY.get(country) or {}
    if code not in accepted:
        raise ValueError(
            f'address carries provinceCode={code!r}, which is not an accepted code '
            f'for {country}. Unvalidated text must never reach provinceCode.')
    return True
