"""Fetch Shopify's authoritative province/zone codes into a tracked dataset.

SOURCE: github.com/Shopify/worldwide, data/regions/<CC>.yml, the `zones:` list.
This is Shopify's own country/region data - the same source its address
validation is built on - not a third-party ISO table and not recalled from
memory. Shopify's province codes follow ISO 3166-2, confirmed in their Province
API docs, but the accepted SET per country is Shopify's to define, so it is
taken from Shopify.

Reads GitHub only. Never contacts the Shopify Admin API, never writes to the
store. Run once, commit the JSON, and every downstream validation is offline.

Why a file rather than a live lookup at import time: 12,096 customers must not
depend on GitHub being reachable, and a province list that can change silently
between a dry run and a real run is a list that can invalidate a dry run
nobody re-read.

Usage:
    python migration/scripts/phase10_fetch_province_codes.py            # full refresh
    python migration/scripts/phase10_fetch_province_codes.py US CA IE   # MERGE these in

A named-country run MERGES into the existing dataset. It used to replace it
wholesale, which meant `... HU` silently reduced 756 codes across 22 countries
to zero - destroying the validation table while reporting success. Merge is the
only safe default for a partial fetch.

Writes migration/schema/shopify_province_codes.json (TRACKED, no PII).
"""
import base64
import datetime
import json
import os
import subprocess
import sys

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'schema', 'shopify_province_codes.json')

# Every country that appears in this migration's address data, plus every
# country the previous hand-written allowlist named. Fetching only what is
# needed keeps the file reviewable; anything absent is handled by the validator
# as "no province data" and omits provinceCode, which is the safe outcome.
DEFAULT_COUNTRIES = sorted({
    # present in the WooCommerce billing/shipping data
    'GB', 'FR', 'IE', 'DE', 'US', 'IT', 'NL', 'ES', 'BE', 'CA',
    'NG', 'NO', 'LT', 'CZ', 'SC', 'AU', 'HU',
    # named by the superseded PROVINCE_CODE_COUNTRIES allowlist
    'JP', 'BR', 'CN', 'IN', 'MX', 'MY', 'AR', 'TH', 'ID', 'KR',
    'PT', 'RO', 'ZA', 'EG',
})


def fetch_region_yaml(country_code):
    """Raw YAML for one country from Shopify/worldwide, via the GitHub API."""
    result = subprocess.run(
        ['gh', 'api', f'repos/Shopify/worldwide/contents/data/regions/{country_code}.yml',
         '--jq', '.content'],
        capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None
    return base64.b64decode(result.stdout.strip()).decode('utf-8', errors='replace')


def parse_zones(yaml_text):
    """[(code, name)] from the top-level `zones:` list.

    Hand-parsed rather than via PyYAML, which is not installed and is not worth
    a new dependency for two keys. The entry shape is NOT consistent across
    countries - Ireland leads with `- name:`, the United States with `- tax:` -
    so an entry is delimited by any line beginning "- " at zero indent, and the
    keys are read wherever they appear inside it:

        zones:                       zones:
        - name: Carlow               - tax: 0.04
          code: CW                     name: Alabama
                                       code: AL
                                       zip_prefixes:
                                       - '350'

    Only `code:`/`name:` at exactly two spaces of indent are taken, so nested
    list items like the zip_prefixes entries above cannot be mistaken for zone
    data, and a deeper nested `code:` cannot either.
    """
    zones, in_zones, current = [], False, None

    def flush():
        if current and current.get('code'):
            zones.append((current['code'], current.get('name', '')))

    quotes = '"\''

    for raw in yaml_text.splitlines():
        if raw.rstrip() == 'zones:':
            in_zones = True
            continue
        if not in_zones:
            continue
        # Comments and blank lines are not structure. Italy carries a
        # zero-indent comment mid-list explaining the Aosta code, which
        # silently truncated this parse to 3 of 107 provinces until it was
        # skipped explicitly.
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        # Any line at zero indent that is not a list item ends the block.
        if raw and not raw.startswith(' ') and not raw.startswith('- '):
            break
        if raw.startswith('- '):
            flush()
            current = {}
            rest = raw[2:].strip()
            if rest.startswith('name:'):
                current['name'] = rest[len('name:'):].strip().strip(quotes)
            elif rest.startswith('code:'):
                current['code'] = rest[len('code:'):].strip().strip(quotes)
            continue
        if current is None:
            continue
        if raw.startswith('  code:') and 'code' not in current:
            current['code'] = raw.strip()[len('code:'):].strip().strip(quotes)
        elif raw.startswith('  name:') and 'name' not in current:
            current['name'] = raw.strip()[len('name:'):].strip().strip(quotes)
    flush()
    return zones


def main():
    explicit = [c.upper() for c in sys.argv[1:]]
    countries = explicit or DEFAULT_COUNTRIES

    # A partial run merges; only a full refresh may rebuild from nothing.
    if explicit and os.path.exists(OUTPUT_PATH):
        existing = json.load(open(OUTPUT_PATH, encoding='utf-8'))
        provinces = dict(existing.get('provinces') or {})
        no_zones = list(existing.get('_countries_with_no_zones') or [])
        preserved_note = existing.get('_verification_note')
        preserved_status = existing.get('_live_verification_status')
        preserved_checked = existing.get('_live_verification_checked')
        print(f'merging {len(explicit)} country/countries into an existing dataset '
              f'of {len(provinces)} with-provinces + {len(no_zones)} without')
    else:
        provinces, no_zones = {}, []
        preserved_note = preserved_status = preserved_checked = None
    failed = []
    for code in countries:
        yaml_text = fetch_region_yaml(code)
        if yaml_text is None:
            failed.append(code)
            print(f'  {code}: FETCH FAILED')
            continue
        zones = parse_zones(yaml_text)
        if not zones:
            if code not in no_zones:
                no_zones.append(code)
            provinces.pop(code, None)
            print(f'  {code}: no zones - provinceCode is not used for this country')
            continue
        provinces[code] = {c: n for c, n in sorted(zones)}
        print(f'  {code}: {len(zones)} zone(s)')

    dataset = {
        '_source': 'github.com/Shopify/worldwide, data/regions/<CC>.yml, `zones:` list',
        '_source_note': (
            "Shopify's own country/region data, not a third-party ISO table. Shopify "
            "province codes follow ISO 3166-2 (confirmed in their Province API docs), "
            "but the accepted set per country is Shopify's to define."),
        '_fetched': datetime.date.today().isoformat(),
        '_countries_requested': sorted(countries),
        '_countries_with_no_zones': sorted(no_zones),
        '_countries_fetch_failed': sorted(failed),
        '_verification_note': preserved_note or (
            'Fetched from Shopify published region data, NOT from the live Admin API. '
            'See docs/RISK_REGISTER.md #43 - live cross-check is an accepted '
            'limitation, not evidence of an error.'),
        'provinces': provinces,
    }
    # A refresh must not silently discard the accepted-limitation record.
    if preserved_status:
        dataset['_live_verification_status'] = preserved_status
    if preserved_checked:
        dataset['_live_verification_checked'] = preserved_checked

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(dataset, f, indent=2, sort_keys=True)

    total = sum(len(v) for v in provinces.values())
    print(f'\nWrote {OUTPUT_PATH}')
    print(f'  {len(provinces)} country/countries with provinces, {total} codes total')
    print(f'  {len(no_zones)} country/countries with none: {sorted(no_zones)}')
    if failed:
        print(f'  FAILED: {sorted(failed)} - investigate rather than shipping a gap')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
