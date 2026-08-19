"""
Phase 10 live-check reporting-integrity regression tests: ACCESS_DENIED must
NEVER be reported as a successful live check.

The defect this guards against: fetch_live_customer_emails() caught Shopify's
ACCESS_DENIED response, returned an empty set, and let the caller record
live_shopify_check_status = "OK" with live_shopify_customers_checked_against = 0
- which reads as "we checked live Shopify and there are 0 customers" when in
fact access was denied and nothing was checked. That false claim was persisted
into reports/phase10_customer_statistics.json, a tracked file.

All local - graphql_request is monkeypatched, so no Shopify credentials and no
network calls are needed. No real customer data is used; every email below is
synthetic (example.com, reserved by RFC 2606).

Run: python migration/scripts/test_phase10_live_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import phase10_customer_dry_run as dr

results = []


def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))
    print(f'{"PASS" if condition else "FAIL"} - {name} {detail}')


def with_stub(response_sequence):
    """Replace graphql_request with one returning canned responses in order."""
    calls = {'n': 0}

    def stub(domain, token, api_version, query, variables=None):
        i = min(calls['n'], len(response_sequence) - 1)
        calls['n'] += 1
        return response_sequence[i]

    dr.graphql_request = stub
    return calls


ACCESS_DENIED = {
    'errors': [{
        'message': 'Access denied for customers field.',
        'extensions': {'code': 'ACCESS_DENIED'},
        'path': ['customers'],
    }]
}


def live_status_for(response_sequence):
    """Reproduce main()'s live-check block exactly, without parsing dump.sql."""
    with_stub(response_sequence)
    live_emails = set()
    status = 'NOT_CONFIGURED'
    try:
        live_emails = dr.fetch_live_customer_emails('x.myshopify.com', 'tok', '2026-07')
        status = 'OK'
    except Exception as e:  # noqa: BLE001 - mirrors main()'s handler
        status = f'FAILED: {e}'
    return status, live_emails


def test_1_access_denied_is_not_ok():
    status, emails = live_status_for([ACCESS_DENIED])
    check('TEST 1: ACCESS_DENIED does not report OK', status != 'OK', f'-> {status!r}')
    check('TEST 1: status is exactly "FAILED: ACCESS_DENIED"',
          status == 'FAILED: ACCESS_DENIED', f'-> {status!r}')
    check('TEST 1: denied check yields no email set to reason from', not emails)


def test_2_successful_check_reports_ok():
    ok = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [{'node': {'email': 'A@Example.com'}}, {'node': {'email': 'b@example.com'}}]}}}
    status, emails = live_status_for([ok])
    check('TEST 2: successful access reports OK', status == 'OK', f'-> {status!r}')
    check('TEST 2: returns the real live email set', emails == {'a@example.com', 'b@example.com'},
          f'-> {sorted(emails)}')


def test_3_empty_store_is_genuinely_zero():
    ok_empty = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'edges': []}}}
    status, emails = live_status_for([ok_empty])
    check('TEST 3: a genuinely empty store still reports OK', status == 'OK', f'-> {status!r}')
    check('TEST 3: genuine zero is distinguishable from denied zero',
          status == 'OK' and len(emails) == 0)


def test_4_other_graphql_errors_also_fail_loudly():
    throttled = {'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED'}}]}
    status, _ = live_status_for([throttled])
    check('TEST 4: non-ACCESS_DENIED GraphQL errors do not report OK', status != 'OK', f'-> {status!r}')
    check('TEST 4: failure reason is surfaced, not swallowed', status.startswith('FAILED:'), f'-> {status!r}')


def test_5_pagination_still_works():
    page1 = {'data': {'customers': {
        'pageInfo': {'hasNextPage': True, 'endCursor': 'CUR1'},
        'edges': [{'node': {'email': 'p1@example.com'}}]}}}
    page2 = {'data': {'customers': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'edges': [{'node': {'email': 'p2@example.com'}}]}}}
    status, emails = live_status_for([page1, page2])
    check('TEST 5: multi-page live check still reports OK', status == 'OK', f'-> {status!r}')
    check('TEST 5: all pages collected', emails == {'p1@example.com', 'p2@example.com'},
          f'-> {sorted(emails)}')


def main():
    original = dr.graphql_request
    try:
        test_1_access_denied_is_not_ok()
        test_2_successful_check_reports_ok()
        test_3_empty_store_is_genuinely_zero()
        test_4_other_graphql_errors_also_fail_loudly()
        test_5_pagination_still_works()
    finally:
        dr.graphql_request = original

    failed = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} passed.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
