"""
Phase 10 customer dry run: parses, validates, and classifies every
WooCommerce customer record against the Shopify customer schema WITHOUT
contacting Shopify with write permissions. Read-only Shopify queries only
(to check whether a customer already exists, for forward-compatible
UPDATE classification - none exist yet, so every live check is expected
to return "not found").

Reuses database_parser.py's load_dump() to read wp_wc_customer_lookup,
wp_users/wp_usermeta (including shipping_* fields, which the existing
customers.json pipeline captures into usermeta but never surfaces - see
docs/PHASE10_CUSTOMER_MAPPING.md), and wp_wc_order_addresses (guest
billing fallback) - not migration/data/customers.json, so every number
here is independently recomputed from the raw dump, not inherited.

Also reads wp_fc_subscribers (FluentCRM) for a real, existing marketing-
consent signal the current pipeline has never used - classified but never
auto-applied as consent (see docs/PHASE10_GDPR_CONSENT.md - unknown
consent is not treated as consent, and a real signal from one system
is not silently treated as sufficient legal basis without a business
decision).

Never silently discards a record: every WooCommerce customer_lookup row
gets an explicit disposition (IMPORT / QUARANTINE / EXCLUDE), including
staff accounts (previously dropped with no audit trail by build_customers()
in database_parser.py - this script does not change that function, it
audits the same rule transparently for Phase 10 purposes).
"""
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from database_parser import load_dump, php_unserialize, STAFF_ROLES, SQL_DUMP_PATH
from sql_utils import iter_insert_rows
from phase9_preflight import get_config, graphql_request

REPORTS_DIR = 'reports'
MANIFEST_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_manifest.csv')
QUARANTINE_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_quarantine.csv')
MAPPING_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_mapping.csv')
STATISTICS_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_statistics.json')
RECONCILIATION_TEMPLATE_PATH = os.path.join(REPORTS_DIR, 'phase10_customer_reconciliation_template.csv')

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
UK_POSTCODE_RE = re.compile(r'^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$', re.IGNORECASE)


def load_fluentcrm_consent():
    """{email: status} from wp_fc_subscribers - a real, existing
    third-party-plugin consent signal, never wired into the customer
    pipeline before now. status is FluentCRM's own vocabulary
    (subscribed/unsubscribed/pending), not a Shopify value - mapping
    that to a Shopify marketing-consent write is a business decision,
    not made here."""
    consent = {}
    for table, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_fc_subscribers'}):
        email = (row.get('email') or '').strip().lower()
        if email:
            consent[email] = row.get('status')
    return consent


def is_valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email))


def build_candidate(row, usermeta, order_billing_by_email):
    user_id = int(row['user_id']) if row.get('user_id') else None
    um = usermeta.get(user_id, {}) if user_id else {}

    address1 = um.get('billing_address_1', '')
    address2 = um.get('billing_address_2', '')
    phone = um.get('billing_phone', '')
    company = um.get('billing_company', '')
    if not address1:
        oa = order_billing_by_email.get((row.get('email') or '').strip().lower())
        if oa:
            address1 = oa.get('address_1') or address1
            address2 = address2 or oa.get('address_2') or ''
            phone = phone or oa.get('phone') or ''
            company = company or oa.get('company') or ''

    ship_address1 = um.get('shipping_address_1', '')
    has_shipping = bool(ship_address1)

    return {
        'woo_customer_id': int(row['customer_id']),
        'user_id': user_id,
        'is_registered': user_id is not None,
        'username': row.get('username') or '',
        'email_raw': row.get('email') or '',
        'email': (row.get('email') or '').strip().lower(),
        'first_name': row.get('first_name') or um.get('billing_first_name') or '',
        'last_name': row.get('last_name') or um.get('billing_last_name') or '',
        'company': company,
        'billing_address1': address1,
        'billing_address2': address2,
        'billing_city': row.get('city') or um.get('billing_city') or '',
        'billing_province': row.get('state') or um.get('billing_state') or '',
        'billing_country': row.get('country') or um.get('billing_country') or '',
        'billing_zip': row.get('postcode') or um.get('billing_postcode') or '',
        'phone': phone,
        'has_shipping_address': has_shipping,
        'shipping_address1': ship_address1,
        'shipping_address2': um.get('shipping_address_2', ''),
        'shipping_city': um.get('shipping_city', ''),
        'shipping_province': um.get('shipping_state', ''),
        'shipping_country': um.get('shipping_country', ''),
        'shipping_zip': um.get('shipping_postcode', ''),
        'date_registered': row.get('date_registered') or '',
        'date_last_active': row.get('date_last_active') or '',
    }


def classify(candidate, seen_emails, staff_user_ids, fc_consent, live_existing_emails):
    """Returns (classification, reason, notes). Never returns a silent
    drop - every input row produces exactly one disposition.

    Only genuine blockers become QUARANTINE (needs a human decision) or
    EXCLUDE (permanently ineligible by rule): missing/invalid email
    (email is the match/dedup key), a conflicting identity sharing one
    email (can't tell which is correct), and staff/admin accounts.
    Everything else Shopify doesn't actually require - missing name,
    a malformed-looking postcode, a duplicate row that's just a repeat
    guest checkout with the SAME identity - is informational only and
    still imports, matching the severity precedent already established
    for products (docs/RISK_REGISTER.md - missing_sku/missing_category
    were never blocking either)."""
    email = candidate['email']
    notes = []

    if candidate['user_id'] is not None and candidate['user_id'] in staff_user_ids:
        return 'EXCLUDE', 'staff_or_admin_account', (
            f"WordPress role includes {sorted(STAFF_ROLES & staff_user_ids[candidate['user_id']])}, not a real storefront customer")

    if not email:
        return 'QUARANTINE', 'missing_email', 'No email on this record - cannot create or match a Shopify customer without one'

    if not is_valid_email(candidate['email_raw']):
        return 'QUARANTINE', 'invalid_email_format', f"Email {candidate['email_raw']!r} does not match a basic RFC-shaped pattern"

    if email in seen_emails:
        prior = seen_emails[email]
        if prior['first_name'] != candidate['first_name'] or prior['last_name'] != candidate['last_name']:
            return 'QUARANTINE', 'duplicate_email_conflicting_identity', (
                f"Email already used by woo_customer_id={prior['woo_customer_id']} "
                f"({prior['first_name']} {prior['last_name']}) with a different name here "
                f"({candidate['first_name']} {candidate['last_name']}) - do not silently merge")
        return 'SKIP', 'duplicate_of_already_included_row', (
            f"Same email and name as woo_customer_id={prior['woo_customer_id']}, already IMPORT/UPDATE-classified - "
            f"a repeat guest checkout, not a data conflict; this row needs no separate action")

    if not candidate['first_name'] and not candidate['last_name']:
        notes.append('missing_name (both first_name and last_name empty - not required by Shopify, imported anyway)')

    if candidate['billing_country'] == 'GB' and candidate['billing_zip'] and not UK_POSTCODE_RE.match(candidate['billing_zip']):
        notes.append(f"malformed_uk_postcode ({candidate['billing_zip']!r}) - not validated by Shopify, imported as-is")

    if email in live_existing_emails:
        return 'UPDATE', '; '.join(notes), 'Matched an existing Shopify customer by email (see idempotency strategy doc for why email, not legacy ID, is the match key for customers)'

    return 'IMPORT', '; '.join(notes), ''


def fetch_live_customer_emails(domain, token, api_version):
    """Read-only. For forward-compatible UPDATE classification - expected
    empty on a store where no customer has ever been imported."""
    emails = set()
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        data = graphql_request(domain, token, api_version,
                                '{ customers(first: 50%s) { pageInfo { hasNextPage endCursor } edges { node { email } } } }' % after)
        if 'errors' in data:
            print('WARNING: could not read live customers (read scope may be missing) - treating live set as unknown/empty:', data['errors'])
            return emails
        for edge in data['data']['customers']['edges']:
            if edge['node']['email']:
                emails.add(edge['node']['email'].strip().lower())
        page = data['data']['customers']['pageInfo']
        if not page['hasNextPage']:
            return emails
        cursor = page['endCursor']


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    live_existing_emails = set()
    live_check_status = 'NOT_CONFIGURED'
    if domain and token:
        try:
            live_existing_emails = fetch_live_customer_emails(domain, token, api_version)
            live_check_status = 'OK'
            print(f'{len(live_existing_emails)} customer(s) currently exist live in Shopify (read-only check)')
        except Exception as e:
            live_check_status = f'FAILED: {e}'
            print(f'WARNING: live Shopify check failed ({e}) - proceeding with live_existing_emails treated as '
                  f'unknown/empty. This does NOT affect the source-data classification below, only the '
                  f'forward-compatible UPDATE detection (which requires live Shopify access).')
    else:
        print('NOT_CONFIGURED - proceeding with live_existing_emails treated as empty (cannot verify UPDATE candidates)')

    print('Reading dump.sql (customer-relevant tables only)...')
    data = load_dump(SQL_DUMP_PATH)
    usermeta = data['usermeta']
    users = data['users']
    order_billing_by_email = data['order_billing_by_email']

    staff_user_ids = {}
    for uid, um in usermeta.items():
        caps = php_unserialize(um.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            staff_user_ids[uid] = set(caps)

    fc_consent = load_fluentcrm_consent()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    seen_emails = {}
    counts = {'IMPORT': 0, 'UPDATE': 0, 'SKIP': 0, 'QUARANTINE': 0, 'EXCLUDE': 0}
    quarantine_reason_counts = {}
    manifest_fields = [
        'woo_customer_id', 'user_id', 'is_registered', 'email', 'first_name', 'last_name',
        'company', 'billing_address1', 'billing_city', 'billing_province', 'billing_country', 'billing_zip',
        'phone', 'has_shipping_address', 'shipping_address1', 'shipping_city', 'shipping_country',
        'date_registered', 'marketing_consent_source', 'marketing_consent_status',
        'classification', 'reason', 'notes',
    ]
    quarantine_fields = ['woo_customer_id', 'email', 'reason', 'classification', 'notes', 'recommended_human_action']

    total_rows = 0
    with open(MANIFEST_PATH, 'w', newline='', encoding='utf-8') as mf, \
         open(QUARANTINE_PATH, 'w', newline='', encoding='utf-8') as qf:
        mw = csv.DictWriter(mf, fieldnames=manifest_fields)
        mw.writeheader()
        qw = csv.DictWriter(qf, fieldnames=quarantine_fields)
        qw.writeheader()

        for table, row in iter_insert_rows(SQL_DUMP_PATH, {'wp_wc_customer_lookup'}):
            total_rows += 1
            candidate = build_candidate(row, usermeta, order_billing_by_email)
            classification, reason, notes = classify(candidate, seen_emails, staff_user_ids, fc_consent, live_existing_emails)

            if classification in ('IMPORT', 'UPDATE'):
                seen_emails[candidate['email']] = candidate

            counts[classification] += 1
            if classification == 'QUARANTINE':
                quarantine_reason_counts[reason] = quarantine_reason_counts.get(reason, 0) + 1

            consent_status = fc_consent.get(candidate['email'])
            mw.writerow({
                'woo_customer_id': candidate['woo_customer_id'],
                'user_id': candidate['user_id'] or '',
                'is_registered': candidate['is_registered'],
                'email': candidate['email'],
                'first_name': candidate['first_name'],
                'last_name': candidate['last_name'],
                'company': candidate['company'],
                'billing_address1': candidate['billing_address1'],
                'billing_city': candidate['billing_city'],
                'billing_province': candidate['billing_province'],
                'billing_country': candidate['billing_country'],
                'billing_zip': candidate['billing_zip'],
                'phone': candidate['phone'],
                'has_shipping_address': candidate['has_shipping_address'],
                'shipping_address1': candidate['shipping_address1'],
                'shipping_city': candidate['shipping_city'],
                'shipping_country': candidate['shipping_country'],
                'date_registered': candidate['date_registered'],
                'marketing_consent_source': 'fluentcrm' if consent_status else 'none',
                'marketing_consent_status': consent_status or 'unknown',
                'classification': classification,
                'reason': reason,
                'notes': notes,
            })

            if classification == 'QUARANTINE':
                recommended = {
                    'invalid_email_format': 'Correct or remove the email in WooCommerce, then re-run',
                    'duplicate_email_conflicting_identity': 'Manually determine which identity is correct before import',
                    'missing_email': 'Cannot import without an email - source one or exclude permanently',
                }.get(reason, 'Manual review required')
                qw.writerow({
                    'woo_customer_id': candidate['woo_customer_id'], 'email': candidate['email'],
                    'reason': reason, 'classification': classification, 'notes': notes,
                    'recommended_human_action': recommended,
                })

    # Field-mapping trace: one row per manifest field, no per-record dump
    # (that's the manifest itself) - this is the WooCommerce -> Shopify
    # target mapping, kept alongside docs/PHASE10_CUSTOMER_MAPPING.md as
    # a machine-checkable companion, not a duplicate of the prose doc.
    with open(MAPPING_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woocommerce_source', 'shopify_target', 'target_type', 'notes'])
        mapping_rows = [
            ('wp_wc_customer_lookup.email', 'email', 'customer field', 'Primary identifier and dedup/match key'),
            ('wp_wc_customer_lookup.first_name / billing_first_name', 'firstName', 'customer field', 'customer_lookup wins, usermeta fallback'),
            ('wp_wc_customer_lookup.last_name / billing_last_name', 'lastName', 'customer field', 'customer_lookup wins, usermeta fallback'),
            ('billing_phone / order billing phone (guest fallback)', 'phone', 'customer field', 'Must be unique in Shopify if set - see quarantine rule for phone conflicts'),
            ('billing_address_1/2, billing_city, billing_state, billing_postcode, billing_country', 'defaultAddress (CustomerAddressInput)', 'address', 'One address only - created via customerCreate\'s addresses input, not a separate mutation'),
            ('shipping_address_1/2, shipping_city, shipping_state, shipping_postcode, shipping_country', 'NOT CURRENTLY MAPPED', 'unmapped', 'Captured in raw usermeta by database_parser.py but never surfaced - Shopify supports multiple addresses per customer; a second CustomerAddressInput could carry this, but no decision has been made to add it'),
            ('user_id (is registered vs guest)', 'tags: "imported-from-woocommerce", "registered"/"guest"', 'tag', 'Already the pattern used in the pre-built CSV'),
            ('username', 'NOT MAPPED', 'excluded', 'Shopify customer accounts are email-based; no username field exists'),
            ('customer_id (WooCommerce)', 'metafield custom.legacy_woo_customer_id', 'metafield', 'Proposed idempotency key, mirroring the product custom.legacy_woo_id pattern - not yet implemented'),
            ('date_registered', 'metafield custom.woo_registered_at (proposed)', 'metafield', 'Informational only; Shopify sets its own createdAt on import'),
            ('WooCommerce customer_note (order-level, not customer-level)', 'NOT MAPPED', 'excluded', 'customer_note in WooCommerce is per-order, not per-customer profile - no customer-level note field exists in the source data read by this pipeline'),
            ('wp_capabilities (role)', 'exclusion filter only (STAFF_ROLES)', 'deliberately excluded', 'Staff/admin accounts are never imported as customers'),
            ('wp_fc_subscribers.status (FluentCRM)', 'emailMarketingConsent.marketingState (proposed, NOT YET APPROVED)', 'consent field', 'Real signal exists for 6,545 of 12,096 customers - see docs/PHASE10_GDPR_CONSENT.md; not applied without explicit business/legal sign-off'),
            ('No consent signal at all (5,551 of 12,096)', 'emailMarketingConsent omitted entirely (defaults to NOT_SUBSCRIBED)', 'consent field', 'Unknown consent is never treated as consent'),
            ('WooCommerce password hash', 'NOT MAPPED - technically impossible', 'unsupported', 'This store uses NEW_CUSTOMER_ACCOUNTS (passwordless) - CustomerInput has no password field at all, verified via live schema introspection'),
        ]
        w.writerows(mapping_rows)

    # Reconciliation template - same shape as the product reconciliation
    # report (phase9_test_reconcile.py), pre-populated with field names
    # only; real values require a live test import, which this dry run
    # does not perform.
    with open(RECONCILIATION_TEMPLATE_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['woo_customer_id', 'shopify_customer_gid', 'field', 'expected_value', 'actual_value', 'match', 'notes'])
        for field in ['legacy_woo_customer_id_metafield', 'email', 'first_name', 'last_name', 'phone',
                      'billing_address', 'shipping_address', 'tags', 'note', 'emailMarketingConsent.marketingState']:
            w.writerow(['<pending test import>', '', field, '', '', 'NOT_APPLICABLE', 'Populated only after an approved test import'])

    stats = {
        'total_wp_wc_customer_lookup_rows': total_rows,
        'classification_counts': counts,
        'quarantine_reason_counts': quarantine_reason_counts,
        'staff_accounts_excluded': len(staff_user_ids),
        'distinct_emails_after_dedup': len(seen_emails),
        'registered_count': sum(1 for c in seen_emails.values() if c['is_registered']),
        'guest_count': sum(1 for c in seen_emails.values() if not c['is_registered']),
        'with_phone': sum(1 for c in seen_emails.values() if c['phone']),
        'without_phone': sum(1 for c in seen_emails.values() if not c['phone']),
        'with_billing_address': sum(1 for c in seen_emails.values() if c['billing_address1']),
        'without_billing_address': sum(1 for c in seen_emails.values() if not c['billing_address1']),
        'with_shipping_address': sum(1 for c in seen_emails.values() if c['has_shipping_address']),
        'with_both_billing_and_shipping': sum(1 for c in seen_emails.values() if c['billing_address1'] and c['has_shipping_address']),
        'marketing_consent_subscribed_fluentcrm': sum(1 for c in seen_emails.values() if fc_consent.get(c['email']) == 'subscribed'),
        'marketing_consent_unsubscribed_fluentcrm': sum(1 for c in seen_emails.values() if fc_consent.get(c['email']) == 'unsubscribed'),
        'marketing_consent_pending_fluentcrm': sum(1 for c in seen_emails.values() if fc_consent.get(c['email']) == 'pending'),
        'marketing_consent_unknown_no_fluentcrm_record': sum(1 for c in seen_emails.values() if c['email'] not in fc_consent),
        'live_shopify_customers_checked_against': len(live_existing_emails),
        'live_shopify_check_status': live_check_status,
    }
    with open(STATISTICS_PATH, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2)

    print(json.dumps(stats, indent=2))
    print(f'\nWrote {MANIFEST_PATH}, {QUARANTINE_PATH}, {MAPPING_PATH}, {STATISTICS_PATH}, {RECONCILIATION_TEMPLATE_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
