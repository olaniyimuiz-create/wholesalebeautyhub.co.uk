"""Verify the province-code dataset against the LIVE Shopify Admin API.

READ-ONLY. Issues queries only; a guard refuses any mutation document.

ACCEPTED LIMITATION (2026-08-22, risk register #43): this cross-check cannot
run against this store. Authentication succeeds, but province lists need
read_shipping (deliveryProfiles) or read_markets (markets), and the app holds
neither. Every other query-root field was checked; none exposes them. Adding a
scope was explicitly ruled out at this stage, so this script reports the
limitation and must never suggest one.

The SOURCE OF TRUTH is Shopify's own published region data
(github.com/Shopify/worldwide, data/regions/<CC>.yml), captured in
migration/schema/shopify_province_codes.json. The absence of a live cross-check
is a gap in independent confirmation - NOT evidence the dataset is incorrect.

The script is kept because the limitation may not be permanent: if the app ever
holds read_shipping for an unrelated reason, this runs unchanged and closes the
gap. Nothing here changes the province transformation rules.

Run: python migration/scripts/phase10_verify_province_codes.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt
from phase9_preflight import get_config, graphql_request

# Shopify exposes provinces per country through the delivery/shipping-zone
# graph. Only countries the shop has added to a shipping zone are returned, so
# an absent country here is not evidence the dataset is wrong.
ZONES_QUERY = '''
{
  deliveryProfiles(first: 10) {
    edges { node { profileLocationGroups { locationGroupZones(first: 50) {
      edges { node { zone { countries {
        code { countryCode } provinces { code name }
      } } } }
    } } } }
  }
}
'''


def main():
    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - no credentials; nothing verified')
        return 2

    if re.match(r'^\s*mutation\b', ZONES_QUERY):
        raise SystemExit('REFUSED: this script issues queries only')

    try:
        data = graphql_request(domain, token, api_version, ZONES_QUERY)
    except Exception as exc:  # noqa: BLE001
        print(f'LIVE CHECK FAILED: {type(exc).__name__}: {str(exc)[:160]}')
        print('The dataset remains UNVERIFIED against the live store. Do not treat '
              'this as a pass - re-run when the credential works.')
        return 1
    if 'errors' in data:
        codes = {(e.get('extensions') or {}).get('code') for e in data['errors']}
        if 'ACCESS_DENIED' in codes:
            # ACCEPTED LIMITATION, recorded 2026-08-22 (risk register #43).
            # Not a failure and not a to-do. Authentication succeeds; the app
            # holds 24 scopes, none of which expose province lists, and every
            # other query-root field was checked - none reaches them either.
            # A scope change was explicitly ruled out at this stage, so this
            # script must NOT suggest one.
            print('LIVE CROSS-CHECK UNAVAILABLE - accepted limitation, not a failure.')
            print()
            print('  Authentication succeeded. Province lists are simply not readable')
            print('  with the scopes this app holds: deliveryProfiles requires')
            print('  read_shipping, markets requires read_markets, and no other query')
            print('  root field exposes them.')
            print()
            print('  SOURCE OF TRUTH: Shopify\'s own published region data')
            print('  (github.com/Shopify/worldwide, data/regions/<CC>.yml), captured in')
            print('  migration/schema/shopify_province_codes.json.')
            print()
            print('  This is a gap in INDEPENDENT CONFIRMATION. It is NOT evidence that')
            print('  the dataset is wrong, and must not be read as such. The province')
            print('  transformation rules are unchanged and stay as ratified:')
            print('    - GB omits provinceCode unconditionally (mandatory)')
            print('    - non-GB validates the value; unrecognised values are omitted +')
            print('      flagged, never coerced')
            print()
            print('  See docs/RISK_REGISTER.md #43. No scope change is being requested.')
            return 2
        print('LIVE CHECK FAILED:', json.dumps(data['errors'])[:300])
        return 1

    live = {}
    for profile in data['data']['deliveryProfiles']['edges']:
        for group in profile['node']['profileLocationGroups']:
            for zone_edge in group['locationGroupZones']['edges']:
                for country in zone_edge['node']['zone']['countries']:
                    code = (country.get('code') or {}).get('countryCode')
                    if not code:
                        continue
                    live.setdefault(code, set()).update(
                        p['code'] for p in (country.get('provinces') or []) if p.get('code'))

    if not live:
        print('No countries are configured in any shipping zone, so the live store '
              'exposes no province lists to compare against. The dataset stays '
              'UNVERIFIED - this is not a pass.')
        return 1

    dataset = rt.PROVINCE_CODES_BY_COUNTRY
    mismatches = []
    for country, live_codes in sorted(live.items()):
        expected = set(dataset.get(country, {}))
        if not expected and not live_codes:
            continue
        missing = live_codes - expected
        extra = expected - live_codes
        status = 'MATCH' if not missing and not extra else 'MISMATCH'
        print(f'  {country}: {status} (live {len(live_codes)}, dataset {len(expected)})')
        if missing:
            print(f'      live has, dataset lacks: {sorted(missing)[:12]}')
        if extra:
            print(f'      dataset has, live lacks: {sorted(extra)[:12]}')
        if status == 'MISMATCH':
            mismatches.append(country)

    print(f'\n{len(live)} country/countries compared, {len(mismatches)} mismatch(es).')
    if mismatches:
        print('Investigate before any address write - do not silently prefer either side.')
        return 1
    print('Dataset agrees with the live store for every country it could compare.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
