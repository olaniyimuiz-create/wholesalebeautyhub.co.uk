"""Phase 10 customer-import pre-flight. READ-ONLY - performs zero mutations.

The Phase 9 equivalent checks product scopes and product reads. This checks
what Phase 10 actually depends on: customer access, the pinned API schema, the
province dataset, the measured rate plan, the reconciled source population, and
- the part a technical pre-flight usually leaves out - whether the business
decisions that gate a customer write have actually been made.

A green technical check on an unapproved migration is a dangerous thing to
print, so this reports both and refuses to call the run READY while any gate is
open.

Never writes. Every document is a query, and _refuse_mutation() rejects any
document whose operation is a mutation before it can be sent. No credential
value is printed, logged, or written - only a prefix, a length, and a hash.

Exit codes:
    0  READY for the next authorized step
    2  NOT READY - open business gates, or a check failed
    1  halted (auth failure, or a technical precondition is broken)

Run: python migration/scripts/phase10_preflight.py
"""
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase9_preflight import get_config, graphql_request

REPORTS = 'reports'
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'schema')

REQUIRED_CUSTOMER_SCOPES = {'read_customers', 'write_customers'}

# Population reconciled and reproduced byte-identically across runs.
EXPECTED = {'total': 13043, 'IMPORT': 12096, 'SKIP': 407, 'QUARANTINE': 539, 'EXCLUDE': 1}

results = []


def record(name, status, detail=''):
    results.append((name, status, detail))
    symbol = {'PASS': 'PASS', 'FAIL': 'FAIL', 'BLOCK': 'GATE', 'INFO': 'INFO'}[status]
    print(f'[{symbol}] {name}' + (f' - {detail}' if detail else ''))
    return status == 'PASS'


def _refuse_mutation(document):
    if re.match(r'^\s*mutation\b', document):
        raise SystemExit('REFUSED: phase10_preflight issues queries only')
    return document


def main():
    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or '2025-01'

    print('Phase 10 pre-flight - READ-ONLY. No customer, address, or metafield '
          'will be created, updated, or deleted.\n')

    # ---------------------------------------------------------------- config
    if not domain or not token:
        record('configuration', 'FAIL',
               'SHOPIFY_STORE_DOMAIN and/or SHOPIFY_ADMIN_API_ACCESS_TOKEN not set')
        return 1
    record('configuration', 'PASS',
           f'store={domain}, api={api_version}, token prefix={token[:6]} len={len(token)} '
           f'sha256[:12]={hashlib.sha256(token.encode()).hexdigest()[:12]}')

    def q(doc):
        _refuse_mutation(doc)
        data = graphql_request(domain, token, api_version, doc)
        if 'errors' in data:
            codes = {(e.get('extensions') or {}).get('code') for e in data['errors']}
            raise RuntimeError(f"GraphQL {sorted(c for c in codes if c)}: "
                               f"{json.dumps(data['errors'])[:160]}")
        return data['data'], data.get('extensions', {})

    # ------------------------------------------------------------ live store
    try:
        shop, _ = q('{ shop { name myshopifyDomain plan { displayName partnerDevelopment } } }')
    except Exception as exc:  # noqa: BLE001
        record('authentication', 'FAIL', f'{type(exc).__name__}: {str(exc)[:120]}')
        print('\nHALTED: cannot authenticate. Nothing further attempted.')
        return 1
    s = shop['shop']
    record('authentication', 'PASS',
           f"{s['name'].strip()!r} ({s['myshopifyDomain']}), plan {s['plan']['displayName']}"
           + (' [development store]' if s['plan']['partnerDevelopment'] else ''))

    try:
        inst, _ = q('{ currentAppInstallation { accessScopes { handle } } }')
        granted = {x['handle'] for x in inst['currentAppInstallation']['accessScopes']}
        missing = REQUIRED_CUSTOMER_SCOPES - granted
        record('customer scopes', 'FAIL' if missing else 'PASS',
               f'missing {sorted(missing)}' if missing
               else f'{len(granted)} granted, including read_customers + write_customers')
    except Exception as exc:  # noqa: BLE001
        record('customer scopes', 'FAIL', str(exc)[:120])

    try:
        state, _ = q('{ customersCount { count } productsCount { count } '
                     'metafieldDefinitions(first: 1, ownerType: CUSTOMER) '
                     '{ edges { node { key } } } }')
        live_customers = state['customersCount']['count']
        defs = len(state['metafieldDefinitions']['edges'])
        record('live customer count', 'PASS' if live_customers == 0 else 'INFO',
               f'{live_customers} customer(s) live'
               + (' - an empty store, as the plan assumes' if live_customers == 0
                  else ' - the plan assumes an empty store; resume logic applies'))
        record('customer metafield definitions', 'PASS',
               f'{defs} defined - none required under the ratified architecture')
        record('products', 'INFO', f"{state['productsCount']['count']} live (Phase 9)")
    except Exception as exc:  # noqa: BLE001
        record('live store state', 'FAIL', str(exc)[:120])

    # ------------------------------------------------------------ API schema
    try:
        import phase10_import_runtime as rt
        contract = rt.load_schema_contract()
        TR = 'kind name ofType { kind name ofType { kind name ofType { kind name } } }'

        def tname(t):
            if not t:
                return '?'
            if t.get('kind') == 'NON_NULL':
                return tname(t.get('ofType')) + '!'
            if t.get('kind') == 'LIST':
                return '[' + tname(t.get('ofType')) + ']'
            return t.get('name') or tname(t.get('ofType'))

        live_types = {}
        for name in contract['input_types']:
            d, _ = q('{ __type(name: "%s") { inputFields { name type { %s } } } }' % (name, TR))
            t = d['__type']
            if t and t.get('inputFields'):
                live_types[name] = {f['name']: tname(f['type']) for f in t['inputFields']}
        drift = rt.detect_schema_drift(live_types, contract)
        record('API schema drift', 'FAIL' if drift else 'PASS',
               f'{len(drift)} finding(s): {drift[:3]}' if drift
               else f'{len(live_types)} input types match the pinned {api_version} contract')
        record('CustomerInput has no addresses field', 'PASS'
               if 'addresses' not in live_types.get('CustomerInput', {}) else 'FAIL',
               'confirmed live - addresses need a separate customerAddressCreate call')
    except Exception as exc:  # noqa: BLE001
        record('API schema drift', 'FAIL', str(exc)[:140])

    # -------------------------------------------------- offline preconditions
    try:
        import phase10_province_validator as pv
        codes = sum(len(v) for v in pv.PROVINCE_CODES_BY_COUNTRY.values())
        data = pv.load_province_codes()
        record('province dataset', 'PASS' if codes == 756 else 'FAIL',
               f'{codes} codes across {len(pv.PROVINCE_CODES_BY_COUNTRY)} countries')
        record('province live cross-check', 'INFO',
               f"{data.get('_live_verification_status')} - risk #43, no scope change requested")
        record('GB provinceCode rule', 'PASS'
               if pv.validate_province_code('GB', 'Surrey')[0] is None else 'FAIL',
               'GB omits provinceCode unconditionally')
    except Exception as exc:  # noqa: BLE001
        record('province dataset', 'FAIL', str(exc)[:120])

    stats_path = os.path.join(REPORTS, 'phase10_customer_statistics.json')
    if os.path.exists(stats_path):
        stats = json.load(open(stats_path, encoding='utf-8'))
        counts = stats['classification_counts']
        ok = (stats['total_wp_wc_customer_lookup_rows'] == EXPECTED['total']
              and all(counts[k] == EXPECTED[k] for k in ('IMPORT', 'SKIP', 'QUARANTINE', 'EXCLUDE')))
        record('source population', 'PASS' if ok else 'FAIL',
               f"{counts['IMPORT']} IMPORT / {counts['SKIP']} SKIP / {counts['QUARANTINE']} "
               f"QUARANTINE / {counts['EXCLUDE']} EXCLUDE of "
               f"{stats['total_wp_wc_customer_lookup_rows']}")
        # The source population is 12,096. The RUN population is not, and the
        # difference is a signed decision rather than a discrepancy - reporting
        # only the first invites someone to plan a 12,096-record run.
        names_path = os.path.join(REPORTS, 'phase10_name_conflict_summary.json')
        if os.path.exists(names_path):
            excluded = json.load(open(names_path, encoding='utf-8')).get(
                'affected_import_customers', 0)
            record('run population under Gate 5', 'PASS',
                   f"{counts['IMPORT'] - excluded} to import "
                   f"({counts['IMPORT']} IMPORT minus {excluded} held back by "
                   f"EXCLUDE_AFFECTED_CUSTOMERS - held, not dropped)")

        prov = stats.get('province_validation', {})
        record('province validation', 'PASS' if prov.get('omitted_invalid_code') == 0 else 'FAIL',
               f"{prov.get('sent')} codes sent, {prov.get('omitted_invalid_code')} invalid")
        record('live check in last dry run', 'PASS'
               if stats.get('live_shopify_check_status') == 'OK' else 'FAIL',
               f"status={stats.get('live_shopify_check_status')}")
    else:
        record('source population', 'FAIL', 'no statistics report - run the dry run first')

    cost_path = os.path.join(REPORTS, 'phase10_mutation_cost_analysis.json')
    if os.path.exists(cost_path):
        cost = json.load(open(cost_path, encoding='utf-8'))
        rp = cost.get('rate_plan', {})
        record('mutation cost', 'PASS' if cost.get('records_created') == 0 else 'FAIL',
               f"{rp.get('measured_mutation_cost')} points/mutation measured, "
               f"{rp.get('sustained_mutations_per_second')}/s sustained, concurrency "
               f"{rp.get('safe_concurrency')}, {cost.get('records_created')} records created")
    else:
        record('mutation cost', 'INFO', 'not measured - rate plan would use assumed 10')

    # ------------------------------------------------------- business gates
    print()
    matrix = os.path.join(os.path.dirname(SCHEMA_DIR), '..', 'docs',
                          'PHASE10_DECISION_MATRIX.md')
    matrix = os.path.normpath(matrix)
    open_gates = []
    if os.path.exists(matrix):
        for line in open(matrix, encoding='utf-8'):
            if not re.match(r'^\| \d+ \|', line):
                continue
            cells = [c.strip() for c in line.split('|')]
            if len(cells) > 8 and cells[7].startswith('**YES**'):
                name = re.sub(r'\*+', '', cells[2])
                open_gates.append(name)
                record(f'business gate: {name}', 'BLOCK', cells[8][:70])
    if not open_gates:
        record('business gates', 'PASS', 'none blocking')

    # ------------------------------------------------------------ conclusion
    failed = [r for r in results if r[1] == 'FAIL']
    blocked = [r for r in results if r[1] == 'BLOCK']
    passed = [r for r in results if r[1] == 'PASS']

    print(f'\n{len(passed)} passed, {len(failed)} failed, {len(blocked)} gate(s) open')
    print('\nSHOPIFY MUTATIONS: 0')

    if failed:
        print('\nNOT READY - a technical check failed:')
        for name, _, detail in failed:
            print(f'  {name}: {detail}')
        return 2
    if blocked:
        print(f'\nNOT READY - technically sound, but {len(blocked)} business gate(s) '
              f'remain open:')
        for name, _, _ in blocked:
            print(f'  {name}')
        print('\nEvery technical precondition passes. Phase 10 is not authorized to '
              'write, and this pre-flight does not grant authorization.')
        return 2
    print('\nREADY for the next authorized step. This is a technical result only - '
          'it is not approval and not execution authorization.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
