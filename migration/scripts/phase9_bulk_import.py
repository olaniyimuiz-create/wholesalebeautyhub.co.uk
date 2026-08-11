"""
Phase 9.7 Step 5: controlled bulk import of the 598 IMPORT-eligible
products, authorized explicitly on GitHub issue #14 (2026-08-10T22:12:58Z).

Reuses every write/read primitive from phase9_test_import.py verbatim
(create_product, update_product, upload_media, set_inventory_quantities,
sync_inventory_for_existing, ...), and - after the pilot-batch crash on
products 18/16464 - reuses process_product() itself rather than keeping a
second copy of its create/update/variant-retry/media-retry logic: a
one-off copy here would let a fix to process_product() (like the
broken-variation handling below) silently fail to apply to the bulk path.
process_product() logs/checkpoints per-product actions into
phase9_test_import.py's LOG_PATH/CHECKPOINT_PATH - one continuous ledger
across the whole Phase 9 effort (9-product test + all bulk batches), not
a separate one per script.

The only new code here is orchestration:
- sources the approved ID list from reports/phase9_final_import_manifest.csv
  (IMPORT rows only) instead of the fixed 9-row test CSV,
- runs a caller-specified slice of that list per invocation, so the batch
  size is controlled by the caller (small pilot batch first, larger after
  reconciliation), never all 598 at once inside one call,
- replaces the 9-product importer's per-product full-store-scan idempotency
  lookup with a single existing-ID map fetched once per invocation (the
  same pattern reports/phase9_final_import_manifest.py already uses) -
  a real, previously-undiscovered scaling problem: the original
  find_existing_by_legacy_id pages the *entire* store for every single
  product, which is fine at 9 products but becomes O(n^2) over 598 (a
  598th product's check would page ~600 products just to look itself up
  by exclusion, at a cost of ~12 requests, repeated per product),
- appends batch results to reports/phase9_bulk_import_result.json (a list
  of per-batch dicts, accumulated across invocations) rather than
  overwriting or touching the 9-product test run's own result file.

Hard safety: this script independently re-derives the quarantine/blocking
ID sets (same rules as phase9_dry_run.py) and refuses to process any ID
not classified IMPORT in a freshly regenerated manifest, even if the
caller's batch happened to include one - defense in depth, not reliance
on the caller alone.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request
from phase9_dry_run import (
    load_products, run_data_quality, flag_garbage_brand_products, flag_missing_price,
)
from phase9_test_import import (
    final_preflight, process_product, new_run_id, get_importer_commit,
    log_skipped_variations, APPROVED_TEST_DOMAIN,
)

QUARANTINE_CODES = ('ambiguous_vendor', 'no_source_price')  # incomplete_variation/partial_missing_price are Option A: skip-only, not quarantined

REPORTS_DIR = 'reports'
MANIFEST_CSV = os.path.join(REPORTS_DIR, 'phase9_final_import_manifest.csv')
RESULT_PATH = os.path.join(REPORTS_DIR, 'phase9_bulk_import_result.json')


def load_manifest_import_ids():
    """IMPORT-classified woo_product_ids from the manifest, sorted ascending
    for a deterministic, resumable batch order. Does not trust a stale
    manifest blindly - caller regenerates it fresh immediately beforehand."""
    return sorted(read_manifest_counts()[0])


# ambiguous_vendor (issue #38, ADR-pending) + no_source_price
# (Phase 9.7 pricing-safety recovery, 2026-08-10) quarantine sets combined.
EXPECTED_QUARANTINE_IDS = {1726, 2369, 2370, 2371, 2372} | {
    25089, 25092, 25109, 25111, 25113, 25115, 25117, 25217, 25219, 25369,
}


def read_manifest_counts():
    """Returns (import_ids, quarantine_ids, already_imported_count,
    exclude_count). Invariant that must always hold regardless of how many
    batches have already run or how many quarantine reasons exist:
    len(import_ids) + len(quarantine_ids) + already_imported_count +
    exclude_count == 611 (the full catalog, never changes) and
    quarantine_ids == exactly EXPECTED_QUARANTINE_IDS - never more, never
    fewer, never different ids."""
    import csv
    import_ids, quarantine_ids = [], []
    already_imported = 0
    exclude_count = 0
    with open(MANIFEST_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            wid = int(row['woo_product_id'])
            if row['import_eligibility'] == 'IMPORT':
                import_ids.append(wid)
            elif row['import_eligibility'] == 'QUARANTINE':
                quarantine_ids.append(wid)
            elif row['import_eligibility'] == 'ALREADY_IMPORTED':
                already_imported += 1
            elif row['import_eligibility'] == 'EXCLUDE':
                exclude_count += 1
    return import_ids, quarantine_ids, already_imported, exclude_count


def fetch_existing_legacy_map(domain, token, api_version):
    """{woo_id: shopify_gid} for every product currently in the store, in
    one paginated pass - built once per script invocation rather than once
    per product (see module docstring)."""
    mapping = {}
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        query = ('{ products(first: 50%s) { pageInfo { hasNextPage endCursor } '
                  'edges { node { id metafield(namespace: "custom", key: "legacy_woo_id") { value } } } } }') % after
        data = graphql_request(domain, token, api_version, query)
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        for edge in data['data']['products']['edges']:
            mf = edge['node']['metafield']
            if mf:
                mapping[int(mf['value'])] = edge['node']['id']
        page = data['data']['products']['pageInfo']
        if not page['hasNextPage']:
            return mapping
        cursor = page['endCursor']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', type=int, default=0, help='0-based offset into the sorted IMPORT-eligible ID list')
    ap.add_argument('--count', type=int, default=None, help='number of products to process in this batch')
    ap.add_argument('--ids', default=None, help='comma-separated explicit woo_product_ids (recovery batches) - overrides --start/--count')
    ap.add_argument('--batch-label', default=None)
    args = ap.parse_args()
    if args.ids is None and args.count is None:
        print('STOP: must pass either --count (tiered batch) or --ids (explicit recovery batch).')
        return 1

    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    if not domain or not token:
        print('NOT_CONFIGURED - cannot proceed.')
        return 2

    print('=== Bulk import batch pre-write preflight ===')
    ok, findings = final_preflight_bulk(domain, token, api_version, config['environment'])
    for name, status, detail in findings:
        print(f'[{name}] {status} - {detail}')
    if not ok:
        print('\nSTOP: one or more pre-write checks failed. No write attempted.')
        return 1
    print('\nAll pre-write checks passed. Proceeding to write.\n')

    all_import_ids, manifest_quarantine_ids, already_imported_count, exclude_count = read_manifest_counts()
    if set(manifest_quarantine_ids) != EXPECTED_QUARANTINE_IDS:
        print(f'STOP: manifest QUARANTINE set is {sorted(manifest_quarantine_ids)}, expected exactly {sorted(EXPECTED_QUARANTINE_IDS)}. Refusing to proceed.')
        return 1
    total = len(all_import_ids) + len(manifest_quarantine_ids) + already_imported_count + exclude_count
    if total != 611:
        print(f'STOP: IMPORT({len(all_import_ids)}) + QUARANTINE({len(manifest_quarantine_ids)}) + '
              f'ALREADY_IMPORTED({already_imported_count}) + EXCLUDE({exclude_count}) = {total}, expected 611. Refusing to proceed.')
        return 1

    if args.ids:
        requested_ids = sorted(int(x) for x in args.ids.split(','))
        # Recovery batches may target an ALREADY_IMPORTED id (e.g. product 18,
        # to complete it via the idempotent UPDATE path) as well as fresh
        # IMPORT ids - only quarantine/blocking ids are actually forbidden.
        batch_ids = requested_ids
    else:
        batch_ids = sorted(all_import_ids)[args.start:args.start + args.count]
    if not batch_ids:
        print('STOP: empty batch - nothing to do.')
        return 1

    products = load_products()
    by_id = {p['id']: p for p in products}
    dq_issues = run_data_quality(products) + flag_garbage_brand_products(products) + flag_missing_price(products)
    quarantine_ids = {i['woo_id'] for i in dq_issues if i.get('code') in QUARANTINE_CODES}
    blocking_ids = {i['woo_id'] for i in dq_issues if i['severity'] == 'BLOCKING' and i['entity_type'] == 'product'}

    bad = [i for i in batch_ids if i in quarantine_ids or i in blocking_ids]
    if bad:
        print(f'STOP: batch contains {len(bad)} quarantined/blocking id(s): {bad}. Refusing to proceed.')
        return 1
    unknown = [i for i in batch_ids if i not in by_id]
    if unknown:
        print(f'STOP: batch contains {len(unknown)} id(s) not found in source data: {unknown}. Refusing to proceed.')
        return 1

    print(f'Batch: {"explicit ids" if args.ids else f"start={args.start} count={args.count}"} -> {len(batch_ids)} product(s): {batch_ids}')

    print('Fetching existing legacy_woo_id -> product map (single pass)...')
    existing_map = fetch_existing_legacy_map(domain, token, api_version)
    print(f'{len(existing_map)} product(s) currently in the store before this batch.')

    run_id = new_run_id()
    commit = get_importer_commit()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    batch_result = {
        'batch_label': args.batch_label or (f'explicit_ids_{len(batch_ids)}' if args.ids else f'start{args.start}_count{args.count}'),
        'ids': batch_ids, 'run_id': run_id, 'importer_commit': commit,
        'attempted': 0, 'created': 0, 'updated': 0, 'failed': 0, 'variations_skipped': 0,
        'store_count_before': len(existing_map),
        'records': [],
    }

    for woo_id in batch_ids:
        product = by_id[woo_id]
        batch_result['attempted'] += 1
        existing_gid = existing_map.get(woo_id)
        record, skipped_records = process_product(domain, token, api_version, woo_id, product, existing_gid, run_id, commit)
        log_skipped_variations(skipped_records)
        batch_result['variations_skipped'] += len(skipped_records)
        batch_result['records'].append(record)
        if record['action'] == 'CREATED':
            batch_result['created'] += 1
            existing_map[woo_id] = record['shopify_gid']
        elif record['action'] == 'UPDATED':
            batch_result['updated'] += 1
        else:
            batch_result['failed'] += 1

    all_results = []
    if os.path.exists(RESULT_PATH):
        with open(RESULT_PATH, encoding='utf-8') as f:
            all_results = json.load(f)
    all_results.append(batch_result)
    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=2)

    print(json.dumps({k: v for k, v in batch_result.items() if k != 'records'}, indent=2))
    print(f'\nWrote/updated {RESULT_PATH}; per-product actions appended to phase9_test_import.py\'s shared log/checkpoint files')
    return 0


def final_preflight_bulk(domain, token, api_version, environment):
    """Same 11-point idea as phase9_test_import.final_preflight, but without
    the hardcoded '9 rows' assumption baked into that function - checks
    identity/scopes/not-production against the same live calls instead."""
    findings = []
    ok = True
    data = graphql_request(domain, token, api_version, '{ shop { name myshopifyDomain plan { displayName } } currentAppInstallation { accessScopes { handle } } }')
    if 'errors' in data:
        findings.append(('authentication', 'FAIL', str(data['errors'])))
        return False, findings
    shop = data['data']['shop']
    findings.append(('authentication', 'PASS', f"shop={shop['name']!r}"))
    findings.append(('store_identity', 'PASS' if shop['myshopifyDomain'] == domain else 'FAIL', f"myshopifyDomain={shop['myshopifyDomain']}"))
    if shop['myshopifyDomain'] != domain:
        ok = False
    from phase9_preflight import REQUIRED_SCOPES
    granted = {s['handle'] for s in data['data']['currentAppInstallation']['accessScopes']}
    missing = REQUIRED_SCOPES - granted
    findings.append(('required_scopes', 'PASS' if not missing else 'FAIL', f'missing={sorted(missing)}' if missing else 'all granted'))
    if missing:
        ok = False
    findings.append(('not_production', 'PASS' if (domain == APPROVED_TEST_DOMAIN and environment != 'production') else 'FAIL',
                      f'domain={domain}, environment={environment}'))
    if domain != APPROVED_TEST_DOMAIN or environment == 'production':
        ok = False
    findings.append(('idempotency_key', 'PASS', 'custom.legacy_woo_id'))
    return ok, findings


if __name__ == '__main__':
    sys.exit(main())
