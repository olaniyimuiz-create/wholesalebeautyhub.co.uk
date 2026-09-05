"""
Phase 9 post-import: classify the 43 recorded inventory failures.

READ-ONLY. Performs zero mutations - there is no `mutation` keyword in this
file. Does not retry anything. Produces a classification + live reconciliation
report so a human can decide what, if anything, needs repair.

Sources of truth:
  - reports/phase9_test_import_log.jsonl  (the authoritative append-only ledger)
  - reports/phase9_final_import_manifest.csv (expected inventory per product)
  - live Shopify, via a FULL product scan

Identity: legacy_woo_id is read from a full scan and matched locally. Shopify's
metafield SEARCH is never used - it was proven to return the wrong product
(searching legacy_woo_id:18 returned "Sylvimak Oil-Free Foundation" when woo 18
is "Vee Beauty Total Coverage Foundation").

Scope note: full location *attributes* (name/isActive) need `read_locations`,
which is not granted. Location *IDs* are readable, and that is sufficient to
classify - the store has a single location, so "location could not be found"
means a wrong ID was sent, not a missing location.

Classification:
  A safe deterministic retry
  B already resolved
  C requires investigation
  D SKU/variant mapping issue
  E Shopify API / inventory-location issue
  F cannot safely repair automatically

Usage: python migration/scripts/phase9_inventory_failure_classify.py
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request

APPROVED_DOMAIN = 'wholesale-beautyhub.myshopify.com'
REPORTS_DIR = 'reports'
LEDGER = os.path.join(REPORTS_DIR, 'phase9_test_import_log.jsonl')
MANIFEST = os.path.join(REPORTS_DIR, 'phase9_final_import_manifest.csv')
OUT_CSV = os.path.join(REPORTS_DIR, 'phase9_inventory_failure_classification.csv')

FAILURE_ACTIONS = ('INVENTORY_SET_FAILED', 'INVENTORY_SYNC_FAILED', 'VARIANT_ERROR')


def load_ledger():
    with open(LEDGER, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def error_signature(err):
    """Collapse a raw Shopify error blob to a stable, human-meaningful signature."""
    e = err or ''
    if 'changeFromQuantity' in e:
        return 'MISSING_changeFromQuantity'
    if 'location could not be found' in e:
        return 'LOCATION_NOT_FOUND'
    if 'Throttled' in e:
        return 'THROTTLED'
    if 'upstream timeout' in e or 'timeout' in e.lower():
        return 'UPSTREAM_TIMEOUT'
    if 'Must specify an option name' in e:
        return 'OPTION_NAME_MISSING'
    return 'OTHER'


def attribute_woo_id(ledger, idx):
    """INVENTORY_SET_FAILED rows carry no woo_id. Attribute to the nearest
    PRECEDING ledger entry that does, and say so explicitly rather than
    inventing a link."""
    for j in range(idx - 1, -1, -1):
        if ledger[j].get('woo_id') is not None:
            return ledger[j]['woo_id'], 'inferred_from_preceding_ledger_entry'
    return None, 'unattributable'


def fetch_live_inventory(domain, token, api_version):
    """Full scan. Returns {woo_id: {...}} plus the set of location IDs seen."""
    q = '''
    query($cursor: String) {
      products(first: 60, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id title
          legacy: metafield(namespace: "custom", key: "legacy_woo_id") { value }
          variants(first: 100) {
            nodes {
              id sku inventoryQuantity
              inventoryItem {
                id tracked
                inventoryLevels(first: 5) {
                  nodes { location { id } quantities(names: ["available"]) { name quantity } }
                }
              }
            }
          }
        }
      }
    }'''
    out, locs, cursor = {}, set(), None
    while True:
        data = graphql_request(domain, token, api_version, q, {'cursor': cursor})
        if 'errors' in data:
            raise RuntimeError(json.dumps(data['errors'])[:400])
        page = data['data']['products']
        for n in page['nodes']:
            if not n['legacy']:
                continue
            variants = n['variants']['nodes']
            for v in variants:
                for lvl in v['inventoryItem']['inventoryLevels']['nodes']:
                    locs.add(lvl['location']['id'])
            out[int(n['legacy']['value'])] = {
                'gid': n['id'], 'title': n['title'],
                'variant_count': len(variants),
                'total_qty': sum(v['inventoryQuantity'] or 0 for v in variants),
                'tracked_count': sum(1 for v in variants if v['inventoryItem']['tracked']),
                'variants': variants,
            }
        if not page['pageInfo']['hasNextPage']:
            return out, locs
        cursor = page['pageInfo']['endCursor']


def classify(sig, action, woo_id, later_success, live, expected_qty):
    """Return (class_letter, rationale, recommended_action)."""
    if action == 'INVENTORY_SYNC_FAILED' and later_success:
        return ('B', f'A later INVENTORY_SYNCED entry exists for woo {woo_id}',
                'None - superseded by a later successful sync')
    if action == 'VARIANT_ERROR' and later_success:
        return ('B', f'A later VARIANT_RETRY_SUCCEEDED entry exists for woo {woo_id}',
                'None - retry already succeeded')
    if sig == 'MISSING_changeFromQuantity':
        return ('B', 'Malformed inventorySetQuantities call (INVALID_FIELD_ARGUMENTS); '
                     'the mutation shape was corrected and inventory later set successfully',
                'None - importer defect already fixed; verify live quantity only')
    if sig == 'LOCATION_NOT_FOUND':
        return ('E', 'Shopify rejected the location ID sent by the importer. The store has '
                     'exactly one location, so this is a wrong/stale ID, not a missing location',
                'Confirm the importer resolves the location ID at runtime; no data repair implied')
    if sig in ('THROTTLED', 'UPSTREAM_TIMEOUT'):
        if live is not None and expected_qty is not None and live == expected_qty:
            return ('B', f'Transient {sig}; live quantity {live} already matches expected {expected_qty}',
                    'None - transient error, end state correct')
        return ('A', f'Transient {sig} - deterministically safe to retry once live state is confirmed',
                'Re-run inventory sync for this product only, after approval')
    if sig == 'OPTION_NAME_MISSING':
        return ('F', 'Source variation has no option name; a value would have to be invented',
                'Quarantine - do not fabricate an option name')
    return ('C', 'Unrecognised error signature', 'Manual investigation required')


def main():
    cfg = get_config()
    domain, token = cfg['domain'], cfg['token']
    api_version = cfg['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - no fabricated result.')
        return 2
    if domain != APPROVED_DOMAIN:
        print(f'IDENTITY MISMATCH: {domain!r} != {APPROVED_DOMAIN!r}. STOP.')
        return 3

    ledger = load_ledger()
    failures = [(i, d) for i, d in enumerate(ledger) if d.get('action') in FAILURE_ACTIONS]
    print(f'Ledger: {len(ledger)} entries, {len(failures)} failure entries')

    synced = {d.get('woo_id') for d in ledger if d.get('action') == 'INVENTORY_SYNCED'}
    retried = {d.get('woo_id') for d in ledger
               if d.get('action') in ('VARIANT_RETRY_SUCCEEDED', 'MEDIA_RETRY_SUCCEEDED')}

    manifest = {}
    with open(MANIFEST, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            manifest[int(r['woo_product_id'])] = r

    print('Full live product scan (metafield search deliberately not used)...')
    live, location_ids = fetch_live_inventory(domain, token, api_version)
    print(f'  {len(live)} live products; {len(location_ids)} distinct location id(s): '
          f'{sorted(location_ids)}')

    rows = []
    for idx, d in failures:
        action = d['action']
        woo = d.get('woo_id')
        attribution = 'explicit_in_ledger' if woo is not None else None
        if woo is None:
            woo, attribution = attribute_woo_id(ledger, idx)
        sig = error_signature(d.get('error', ''))
        m = manifest.get(woo, {})
        lv = live.get(woo)
        expected = m.get('inventory_stock_quantity')
        expected = int(expected) if (expected or '').strip().lstrip('-').isdigit() else None
        # Only compare quantities where the comparison is apples-to-apples. The
        # manifest's inventory_stock_quantity is the PARENT product's figure; for a
        # variable product it is not the sum of its variants' quantities, so summing
        # live variants and differencing would manufacture false mismatches.
        is_simple = (m.get('variant_count') == '1' and lv and lv['variant_count'] == 1)
        if is_simple:
            actual, basis = lv['total_qty'], 'simple_product_1:1'
        else:
            actual, basis = None, 'NOT_COMPARABLE_variable_product_parent_vs_variant_sum'
        later = (action == 'INVENTORY_SYNC_FAILED' and woo in synced) or \
                (action == 'VARIANT_ERROR' and woo in retried)
        cls, why, rec = classify(sig, action, woo, later, actual, expected)
        rows.append({
            'ts': d['ts'], 'action': action, 'error_signature': sig,
            'woo_product_id': woo if woo is not None else '',
            'woo_id_attribution': attribution,
            'title': (m.get('title') or (lv or {}).get('title') or ''),
            'sku': m.get('sku', ''),
            'shopify_product_gid': (lv or {}).get('gid', 'NOT_LIVE'),
            'live_variant_count': (lv or {}).get('variant_count', ''),
            'tracked_variants': (lv or {}).get('tracked_count', ''),
            'source_stock': expected if expected is not None else '',
            'live_total_quantity': actual if actual is not None else '',
            'comparison_basis': basis,
            'live_variants_tracked': (lv or {}).get('tracked_count', ''),
            'difference': (actual - expected) if (actual is not None and expected is not None) else '',
            'manage_stock': m.get('inventory_manage_stock', ''),
            'retry_occurred': 'YES' if later else 'NO',
            'retry_succeeded': 'YES' if later else 'N/A',
            'classification': cls,
            'rationale': why,
            'recommended_action': rec,
            'raw_error': (d.get('error') or '')[:300],
        })

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(OUT_CSV, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    import collections
    print(f'\nWrote {OUT_CSV}')
    print('\nCLASSIFICATION SUMMARY')
    for cls, n in sorted(collections.Counter(r['classification'] for r in rows).items()):
        print(f'  {cls}: {n}')
    print('\nBY ERROR SIGNATURE')
    for sig, n in sorted(collections.Counter(r['error_signature'] for r in rows).items()):
        print(f'  {sig:<28} {n}')

    affected = {r['woo_product_id'] for r in rows if r['woo_product_id'] != ''}
    mism = [r for r in rows if r['difference'] not in ('', 0)]
    print(f'\nAffected products: {len(affected)}')
    print(f'Comparable rows where live inventory != expected: {len(mism)}')
    for r in mism[:15]:
        print(f"   woo {r['woo_product_id']}: expected {r['source_stock']}, "
              f"live {r['live_total_quantity']}, diff {r['difference']}")

    # Store-wide tracking check - independent of the 43 failures, because a
    # failure that left tracking disabled would not necessarily appear above.
    print('\nSTORE-WIDE INVENTORY TRACKING CHECK (all 596 products)')
    off, partial = [], []
    for woo, lv in live.items():
        m = manifest.get(woo)
        if not m or (m.get('inventory_manage_stock') or '').strip().lower() != 'yes':
            continue
        if lv['tracked_count'] == 0:
            off.append((woo, lv['title'][:44], m.get('inventory_stock_quantity')))
        elif lv['tracked_count'] < lv['variant_count']:
            partial.append((woo, lv['title'][:44], f"{lv['tracked_count']}/{lv['variant_count']}"))
    print(f'  manage_stock=yes but NO variant tracked live : {len(off)}')
    for r in off:
        print(f'     woo {r[0]} {r[1]} (source qty {r[2]})')
    print(f'  manage_stock=yes but only SOME variants tracked: {len(partial)}')
    for r in partial:
        print(f'     woo {r[0]} {r[1]} tracked {r[2]}')
    print('\nMUTATIONS PERFORMED: 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
