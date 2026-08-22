"""Phase 10 Step 9 - measure real Shopify mutation cost WITHOUT creating anything.

AUTHORIZED 2026-08-22: cost probe only, zero customers created.

HOW THIS MEASURES A MUTATION WITHOUT PERFORMING ONE
---------------------------------------------------
A GraphQL mutation that fails Shopify's *field validation* still executes as a
GraphQL operation: HTTP 200, `data` present, `userErrors` populated, and
`extensions.cost` returned - but no record is created. Sending `customerCreate`
with an email that cannot possibly be valid therefore yields the exact cost and
throttle figures the rate plan needs, while creating nothing.

This is INFERRED rather than verified: `extensions.cost` is returned on every
Shopify GraphQL response, and a userErrors response is a successful execution,
so cost should be present. If it is not, the probe reports that honestly rather
than substituting an assumption.

SAFETY GUARDS - all of them abort the run, none of them are advisory
-------------------------------------------------------------------
  1. Pre-flight must pass. 401 or ACCESS_DENIED halts immediately, no retry.
  2. Every payload is checked BEFORE sending: the email must be one that cannot
     be valid. A payload that could succeed is never sent.
  3. If any probe returns a non-null created record, the run ABORTS instantly.
     The GID is recorded so a human can decide what to do. Nothing is deleted
     automatically - an unexpected write is not something to tidy away quietly.
  4. No metafields, no addresses, no tags in any probe payload. Nothing that
     could leave a partial artifact.
  5. Probes run strictly one at a time, and any abort stops the rest.

NOT USED: phase10_import_runtime. That module refuses mutation documents by
construction and must keep doing so; this probe carries its own transport
rather than weakening the runtime's guarantee.

NO PASSWORD FIELD is included anywhere - the store uses NEW_CUSTOMER_ACCOUNTS
and CustomerInput has no password field at all.

Writes:
  reports/phase10_mutation_cost_analysis.json   TRACKED - costs, no PII
  reports/phase10_test_import_log.jsonl         append-only probe ledger

Run: python migration/scripts/phase10_mutation_cost_probe.py
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase9_preflight import get_config, graphql_request

ANALYSIS_PATH = os.path.join('reports', 'phase10_mutation_cost_analysis.json')
LEDGER_PATH = os.path.join('reports', 'phase10_test_import_log.jsonl')

# Deliberately unusable. No '@', no domain - Shopify rejects this at field
# validation before any record is created. It is also not a real address, so
# nothing here touches a real person.
UNUSABLE_EMAIL = 'phase10-cost-probe-not-a-valid-email'
NONEXISTENT_CUSTOMER_GID = 'gid://shopify/Customer/1'

COST_FRAGMENT = ''  # cost arrives in extensions, not in the selection set


class ProbeAborted(RuntimeError):
    """A guard fired. The run stops; nothing further is sent."""


def assert_payload_cannot_succeed(document, variables):
    """Refuse to send anything that might actually create a record."""
    email = ((variables or {}).get('input') or {}).get('email')
    if 'customerCreate' in document:
        if email != UNUSABLE_EMAIL:
            raise ProbeAborted(f'refusing to send customerCreate with email={email!r}; '
                               f'only the unusable probe address is permitted')
        if '@' in (email or ''):
            raise ProbeAborted('refusing to send: probe email contains "@" and might validate')
    for forbidden in ('metafields', 'addresses', 'password', 'tags'):
        if forbidden in json.dumps(variables or {}):
            raise ProbeAborted(f'refusing to send: payload contains {forbidden!r}')
    return True


def extract_cost(response):
    cost = (response.get('extensions') or {}).get('cost') or {}
    throttle = cost.get('throttleStatus') or {}
    return {
        'requested_cost': cost.get('requestedQueryCost'),
        'actual_cost': cost.get('actualQueryCost'),
        'currently_available': throttle.get('currentlyAvailable'),
        'maximum_available': throttle.get('maximumAvailable'),
        'restore_rate': throttle.get('restoreRate'),
    }


def created_record_gid(response, root_field):
    """The GID of anything this probe accidentally created, or None."""
    payload = ((response.get('data') or {}).get(root_field) or {})
    for key in ('customer', 'address', 'node'):
        obj = payload.get(key)
        if isinstance(obj, dict) and obj.get('id'):
            return obj['id']
    return None


def log(entry):
    directory = os.path.dirname(LEDGER_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(LEDGER_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


PROBES = [
    {
        'name': 'customerCreate',
        'root': 'customerCreate',
        'document': ('mutation probeCustomerCreate($input: CustomerInput!) '
                     '{ customerCreate(input: $input) '
                     '{ customer { id } userErrors { field message } } }'),
        'variables': {'input': {'email': UNUSABLE_EMAIL}},
    },
    {
        'name': 'customerUpdate',
        'root': 'customerUpdate',
        'document': ('mutation probeCustomerUpdate($input: CustomerInput!) '
                     '{ customerUpdate(input: $input) '
                     '{ customer { id } userErrors { field message } } }'),
        # A customer id that does not exist: the update cannot land anywhere.
        'variables': {'input': {'id': NONEXISTENT_CUSTOMER_GID,
                                'email': UNUSABLE_EMAIL}},
    },
    {
        'name': 'customerAddressCreate',
        'root': 'customerAddressCreate',
        'document': ('mutation probeAddressCreate($customerId: ID!, $address: MailingAddressInput!) '
                     '{ customerAddressCreate(customerId: $customerId, address: $address) '
                     '{ address { id } userErrors { field message } } }'),
        'variables': {'customerId': NONEXISTENT_CUSTOMER_GID,
                      'address': {'address1': 'probe', 'countryCode': 'GB'}},
    },
]


def main():
    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or '2025-01'
    started = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if not domain or not token:
        print('NOT_CONFIGURED - no credentials. Nothing sent.')
        return 2

    # ---- Pre-flight. Read-only. Halt on 401 / ACCESS_DENIED, no retry. ----
    print('Pre-flight (read-only)...')
    try:
        pre = graphql_request(domain, token, api_version,
                              '{ shop { name } currentAppInstallation '
                              '{ accessScopes { handle } } customersCount { count } }')
    except Exception as exc:  # noqa: BLE001
        print(f'HALT: pre-flight failed - {type(exc).__name__}: {str(exc)[:140]}')
        log({'ts': started, 'stage': 'preflight', 'status': 'HALT_AUTH_FAILURE'})
        return 1
    if 'errors' in pre:
        codes = {(e.get('extensions') or {}).get('code') for e in pre['errors']}
        print(f'HALT: pre-flight returned {sorted(c for c in codes if c)}')
        log({'ts': started, 'stage': 'preflight', 'status': 'HALT_ACCESS_DENIED'})
        return 1

    scopes = {s['handle'] for s in pre['data']['currentAppInstallation']['accessScopes']}
    customers_before = pre['data']['customersCount']['count']
    print(f"  shop {pre['data']['shop']['name'].strip()!r}, {len(scopes)} scopes, "
          f'{customers_before} customer(s) before probing')
    if 'write_customers' not in scopes:
        print('HALT: write_customers not granted; the probe would fail for the wrong reason')
        return 1

    results, aborted = [], None
    for probe in PROBES:
        assert_payload_cannot_succeed(probe['document'], probe['variables'])
        ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f"\nprobe: {probe['name']}")
        try:
            resp = graphql_request(domain, token, api_version,
                                   probe['document'], probe['variables'])
        except Exception as exc:  # noqa: BLE001
            print(f'  transport failure: {type(exc).__name__}: {str(exc)[:120]}')
            log({'ts': ts, 'stage': probe['name'], 'status': 'TRANSPORT_ERROR'})
            continue

        gid = created_record_gid(resp, probe['root'])
        if gid:
            aborted = (probe['name'], gid)
            print(f'  *** ABORT: this probe CREATED a record: {gid}')
            log({'ts': ts, 'stage': probe['name'], 'status': 'ABORT_RECORD_CREATED',
                 'created_gid': gid})
            break

        cost = extract_cost(resp)
        errs = ((resp.get('data') or {}).get(probe['root']) or {}).get('userErrors') or []
        top = resp.get('errors') or []
        outcome = ('REJECTED_BY_VALIDATION' if errs else
                   'GRAPHQL_ERROR' if top else 'NO_ERROR_BUT_NOTHING_CREATED')
        print(f'  outcome        : {outcome}')
        print(f"  requested cost : {cost['requested_cost']}")
        print(f"  actual cost    : {cost['actual_cost']}")
        print(f"  bucket         : {cost['currently_available']}/{cost['maximum_available']} "
              f"@ {cost['restore_rate']}/s")
        if errs:
            print(f"  userErrors     : {[e.get('field') for e in errs]}")

        record = {'ts': ts, 'stage': probe['name'], 'status': outcome,
                  'record_created': False, **cost}
        results.append({'mutation': probe['name'], 'outcome': outcome, **cost})
        log(record)

    # ---- Confirm nothing was created, from the store itself ----
    after = graphql_request(domain, token, api_version, '{ customersCount { count } }')
    customers_after = (after.get('data') or {}).get('customersCount', {}).get('count')
    print(f'\ncustomers before {customers_before}, after {customers_after}')

    measured = [r for r in results if r.get('actual_cost') is not None]
    create = next((r for r in measured if r['mutation'] == 'customerCreate'), None)
    analysis = {
        'measured_at': started,
        'api_version': api_version,
        'method': ('field-validation-rejected mutations; cost extensions returned, '
                   'no record created'),
        'customers_before': customers_before,
        'customers_after': customers_after,
        'records_created': 0 if customers_after == customers_before else 'UNEXPECTED',
        'probes': results,
        'aborted': {'mutation': aborted[0], 'created_gid': aborted[1]} if aborted else None,
    }

    if create and create.get('actual_cost') and create.get('restore_rate'):
        actual = float(create['actual_cost'])
        restore = float(create['restore_rate'])
        max_avail = float(create.get('maximum_available') or 0)
        analysis['rate_plan'] = {
            'measured_mutation_cost': actual,
            'previous_assumed_cost': 10,
            'assumption_was': ('correct' if actual == 10 else
                               f'wrong - real cost is {actual}'),
            'sustained_mutations_per_second': round(restore / actual, 2),
            'safe_concurrency': 1,
            'burst_capacity_mutations': int(max_avail // actual) if max_avail else None,
            'customer_creates': 12096,
            'address_creates_option_a': 4713,
            'address_creates_option_b': 5922,
            'estimated_seconds_option_a': round((12096 + 4713) * actual / restore),
            'estimated_seconds_option_b': round((12096 + 5922) * actual / restore),
            'note': ('Concurrency stays 1: the bucket is account-wide, so parallelism '
                     'buys nothing against a fixed restore rate and makes throttle '
                     'recovery and checkpoint ordering harder.'),
        }

    os.makedirs('reports', exist_ok=True)
    json.dump(analysis, open(ANALYSIS_PATH, 'w', encoding='utf-8'), indent=2, sort_keys=True)
    print(f'\nWrote {ANALYSIS_PATH}')
    if analysis.get('rate_plan'):
        rp = analysis['rate_plan']
        print(f"  measured cost {rp['measured_mutation_cost']} "
              f"({rp['assumption_was']}) -> {rp['sustained_mutations_per_second']} mutations/s")
        print(f"  Option A ~{rp['estimated_seconds_option_a'] // 60} min, "
              f"Option B ~{rp['estimated_seconds_option_b'] // 60} min")
    if aborted:
        print('\nRUN ABORTED - a record was created unexpectedly. Nothing was deleted; '
              'a human must decide.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
