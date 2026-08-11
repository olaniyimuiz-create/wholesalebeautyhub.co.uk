"""
Phase 9 controlled test import: writes the 9 approved products
(reports/phase9_test_import_set.csv) to the approved Shopify development
store, and only that store. Authorized by explicit project-owner decision
(see docs/DECISIONS.md ADR-011 / docs/PHASE9_ENVIRONMENT_READINESS.md).

Reuses existing logic rather than re-deriving it:
- migration/scripts/phase9_dry_run.py for product data loading, the
  data-quality/quarantine rules, and price/collection resolution helpers.
- migration/scripts/phase9_preflight.py for .env loading and the GraphQL
  request helper.

Idempotency: before creating a product, queries the store for an existing
product carrying the same custom.legacy_woo_id metafield value. If found,
updates top-level fields, and re-runs the variant/option step ONLY if the
live variant count doesn't match what's expected (i.e. a prior run left it
incomplete, as happened for product 1721's real productOptionsCreate
failure) - a completed product's variants/media are never re-touched, so
this can't create duplicates. If not found, creates the product, its real
option/variants, inventory, and its non-AVIF media.

Inventory: `manage_stock == 'yes'` (checked at variant level for variable
products, product level for simple - the same source field
docs/ARCHITECTURE.md's csv_generator.py already keys off) sets the variant
tracked and its real stock_quantity via inventorySetQuantities at the
store's default location. `manage_stock != 'yes'` leaves the variant
untracked, matching WooCommerce's "always in stock" behaviour - the same
rule already documented for the CSV path, applied here for consistency.

Does not create collections or metaobject/brand definitions - those are
separate, not-yet-authorized issues (#12, #13). Collection membership is
recorded as "intended" only, not assigned, and reported as such.

Never logs, prints, or writes a credential value anywhere.
"""
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from phase9_preflight import get_config, graphql_request, REQUIRED_SCOPES
from phase9_dry_run import (
    load_products, load_collections, run_data_quality,
    flag_garbage_brand_products, flag_missing_price, resolve_collections, build_collection_lookup,
    to_number, is_valid_variation, partition_variations, has_price, partition_by_price,
)

REPORTS_DIR = 'reports'
TEST_SET_CSV = os.path.join(REPORTS_DIR, 'phase9_test_import_set.csv')
APPROVED_TEST_DOMAIN = 'wholesale-beautyhub.myshopify.com'
LOG_PATH = os.path.join(REPORTS_DIR, 'phase9_test_import_log.jsonl')
CHECKPOINT_PATH = os.path.join(REPORTS_DIR, 'phase9_test_import_checkpoint.jsonl')
RESULT_PATH = os.path.join(REPORTS_DIR, 'phase9_test_import_result.json')
SKIPPED_VARIATIONS_PATH = os.path.join(REPORTS_DIR, 'phase9_skipped_variations.jsonl')


def get_importer_commit():
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=os.path.dirname(__file__), text=True
        ).strip()
    except Exception:
        return 'unknown'


def new_run_id():
    return str(uuid.uuid4())


def build_skipped_variation_record(product, v, run_id, commit, classification='BROKEN_VARIATION_SKIPPED', reason=None):
    """classification/reason distinguish *why* a variation was skipped:
    - BROKEN_VARIATION_SKIPPED / empty option value (products 18, 16464)
    - MISSING_PRICE_SKIPPED / NO_SOURCE_PRICE (product 69's 4 variants) -
      explicit store-owner decision, Phase 9.7 pricing-safety recovery,
      2026-08-10: missing price must never become '0.00'."""
    if reason is None:
        reason = 'empty option value' if classification == 'BROKEN_VARIATION_SKIPPED' else 'NO_SOURCE_PRICE'
    return {
        'woo_product_id': product['id'],
        'woo_variation_id': v['id'],
        'product_title': product['title'],
        'variant_title': v['options'][0] if v.get('options') else None,
        'classification': classification,
        'reason': reason,
        'source_regular_price': v.get('regular_price'),
        'source_price': v.get('price'),
        'affected_attribute': product['variation_option_names'][0] if product['variation_option_names'] else None,
        'source_value': v['options'][0] if v.get('options') else None,
        'fields_missing': ['options[0]'] if not is_valid_variation(v) else (['price', 'regular_price'] if not has_price(v) else []),
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'run_id': run_id,
        'importer_commit': commit,
        'action': 'SKIPPED',
        'parent_product_action': 'IMPORT_VALID_VARIATIONS',
    }


def log_skipped_variations(records):
    if not records:
        return
    with open(SKIPPED_VARIATIONS_PATH, 'a', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def log(entry):
    entry['ts'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def checkpoint(woo_id, shopify_gid, action):
    with open(CHECKPOINT_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'woo_id': woo_id, 'shopify_gid': shopify_gid, 'action': action}) + '\n')


def read_test_set_ids():
    import csv
    ids = []
    with open(TEST_SET_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            ids.append(int(row['woo_product_id']))
    return ids


def final_preflight(domain, token, api_version, environment):
    """The 11-point pre-write checklist. Returns (ok, findings)."""
    findings = []
    ok = True

    # 1-2: repo cleanliness is checked by the caller (git status) before this runs.

    # 3-5: auth / identity / scopes, via a real call.
    data = graphql_request(domain, token, api_version, '{ shop { name myshopifyDomain plan { displayName } } currentAppInstallation { accessScopes { handle } } }')
    if 'errors' in data:
        findings.append(('authentication', 'FAIL', str(data['errors'])))
        return False, findings
    shop = data['data']['shop']
    findings.append(('authentication', 'PASS', f"shop={shop['name']!r}"))
    findings.append(('store_identity', 'PASS' if shop['myshopifyDomain'] == domain else 'FAIL',
                      f"myshopifyDomain={shop['myshopifyDomain']}"))
    granted = {s['handle'] for s in data['data']['currentAppInstallation']['accessScopes']}
    missing = REQUIRED_SCOPES - granted
    findings.append(('required_scopes', 'PASS' if not missing else 'FAIL', f'missing={sorted(missing)}' if missing else 'all granted'))
    if missing:
        ok = False

    # 6: target store product count.
    data = graphql_request(domain, token, api_version, '{ products(first: 5) { edges { node { id title } } } }')
    existing_products = data['data']['products']['edges']
    findings.append(('target_store_product_count', 'INFO', f'{len(existing_products)} product(s) currently present (showing up to 5): '
                      + ', '.join(e['node']['title'] for e in existing_products) if existing_products else '0 products - empty store'))

    # 7-8: test CSV contents vs. real source data.
    ids = read_test_set_ids()
    findings.append(('test_csv_count', 'PASS' if len(ids) == 9 else 'FAIL', f'{len(ids)} rows'))
    if len(ids) != 9:
        ok = False
    products = load_products()
    by_id = {p['id']: p for p in products}
    missing_ids = [i for i in ids if i not in by_id]
    findings.append(('test_ids_map_to_source', 'PASS' if not missing_ids else 'FAIL', f'missing={missing_ids}' if missing_ids else 'all 9 found in products.json'))
    if missing_ids:
        ok = False

    # 9: not production.
    findings.append(('not_production', 'PASS' if (domain == APPROVED_TEST_DOMAIN and environment != 'production') else 'FAIL',
                      f'domain={domain}, environment={environment}'))
    if domain != APPROVED_TEST_DOMAIN or environment == 'production':
        ok = False

    # 10: import method.
    findings.append(('import_method', 'PASS', 'Admin API / GraphQL (this script)'))

    # 11: idempotency key.
    findings.append(('idempotency_key', 'PASS', 'custom.legacy_woo_id'))

    return ok, findings


def find_existing_by_legacy_id(domain, token, api_version, woo_id):
    """Store is small (test scope) - page through all products checking the
    legacy_woo_id metafield rather than relying on Shopify's metafield
    search-query syntax, which isn't guaranteed stable across API
    versions. Correct beats clever here."""
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        query = '{ products(first: 50%s) { pageInfo { hasNextPage endCursor } edges { node { id metafield(namespace: "custom", key: "legacy_woo_id") { value } } } } }' % after
        data = graphql_request(domain, token, api_version, query)
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        for edge in data['data']['products']['edges']:
            mf = edge['node']['metafield']
            if mf and mf['value'] == str(woo_id):
                return edge['node']['id']
        page = data['data']['products']['pageInfo']
        if not page['hasNextPage']:
            return None
        cursor = page['endCursor']


def build_tags(product):
    return list(dict.fromkeys(product['categories'] + product['tags']))


def create_product(domain, token, api_version, product):
    mutation = '''
    mutation($input: ProductInput!) {
      productCreate(input: $input) {
        product { id handle status }
        userErrors { field message }
      }
    }
    '''
    variables = {'input': {
        'title': product['title'],
        'descriptionHtml': product['body_html'],
        'vendor': product['vendor'],
        'productType': product.get('product_type', ''),
        'tags': build_tags(product),
        'status': 'ACTIVE' if product['status'] == 'publish' else 'DRAFT',
        'handle': product['handle'],
        'metafields': [{
            'namespace': 'custom', 'key': 'legacy_woo_id',
            'type': 'single_line_text_field', 'value': str(product['id']),
        }],
    }}
    data = graphql_request(domain, token, api_version, mutation, variables)
    return data


def update_product(domain, token, api_version, gid, product):
    mutation = '''
    mutation($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id handle status }
        userErrors { field message }
      }
    }
    '''
    variables = {'input': {
        'id': gid,
        'title': product['title'],
        'descriptionHtml': product['body_html'],
        'vendor': product['vendor'],
        'productType': product.get('product_type', ''),
        'tags': build_tags(product),
        'status': 'ACTIVE' if product['status'] == 'publish' else 'DRAFT',
    }}
    data = graphql_request(domain, token, api_version, mutation, variables)
    return data


def get_default_variant(domain, token, api_version, gid):
    data = graphql_request(domain, token, api_version,
                            '{ product(id: "%s") { variants(first: 1) { edges { node { id } } } } }' % gid)
    return data['data']['product']['variants']['edges'][0]['node']['id']


_LOCATION_ID = None


def get_default_location(domain, token, api_version):
    global _LOCATION_ID
    if _LOCATION_ID is None:
        data = graphql_request(domain, token, api_version, '{ locations(first: 1) { edges { node { id } } } }')
        _LOCATION_ID = data['data']['locations']['edges'][0]['node']['id']
    return _LOCATION_ID


def fetch_current_available_quantities(domain, token, api_version, location_id, inventory_item_ids):
    """Batched (single request, via `nodes`) rather than one query per item -
    needed because inventorySetQuantities requires changeFromQuantity per
    entry (real, live-verified requirement - see set_inventory_quantities).

    Defensive by design, not by afterthought: a nonexistent inventory item
    ID comes back as a null entry in `nodes` (not an error), and a
    malformed/error-only response (e.g. an upstream timeout) has no `data`
    key at all - both were found to crash this function with a bare
    KeyError/AttributeError before test coverage caught it. Missing/null
    entries are treated as quantity 0 (an unknown item has no known stock,
    not a guessed one) rather than raised, since the caller
    (set_inventory_quantities) already checks the mutation's own userErrors
    for anything that actually needs surfacing as a failure."""
    if not inventory_item_ids:
        return {}
    ids_literal = ', '.join(f'"{iid}"' for iid in inventory_item_ids)
    query = '''
    { nodes(ids: [%s]) {
        ... on InventoryItem { id inventoryLevel(locationId: "%s") { quantities(names: ["available"]) { quantity } } }
    } }
    ''' % (ids_literal, location_id)
    data = graphql_request(domain, token, api_version, query)
    nodes = data.get('data', {}).get('nodes') or []
    result = {}
    for node in nodes:
        if not node:
            continue
        level = node.get('inventoryLevel')
        result[node['id']] = level['quantities'][0]['quantity'] if level else 0
    return result


def set_inventory_quantities(domain, token, api_version, location_id, item_quantities):
    """item_quantities: list of (inventoryItemId, quantity). Sets absolute
    'available' quantity - matches WooCommerce's stock_quantity semantics
    (an absolute count), not a relative adjustment.

    Verified against the live store directly (not assumed from docs alone)
    after the first attempt failed - see docs/RISK_REGISTER.md #35 and
    docs/PHASE9_INVENTORY_FIX_REPORT.md for the full trace. Two real,
    current API requirements this mutation enforces on this API version:
    1. `@idempotent(key: $key)` directive is mandatory (a plain `input`
       argument is not enough) - Shopify made this mandatory for inventory
       adjustment/refund mutations as of API 2026-04. A fresh UUID per
       call is correct here: this key protects a single request from being
       double-applied if retried after a network failure, not meant to be
       stable across this script's separate runs - the *absolute-set*
       semantics of this mutation is what makes re-running the whole
       script safe, not key reuse.
    2. Each `InventoryQuantityInput` entry requires `changeFromQuantity`
       (the quantity the caller currently believes is set) - so this
       function first fetches the real current quantity for every item in
       one batched query, then sends that as `changeFromQuantity` alongside
       the real target `quantity`. This is what makes the mutation itself
       idempotent in effect: calling it again just re-observes the (now
       correct) current value and re-asserts the same target, which is a
       verified no-op (tested live: calling twice in a row left the
       quantity unchanged, not doubled).

    Returns (success: bool, response: dict) so callers can accurately
    report failure instead of assuming success - the original version of
    this function returned only the raw response, which every caller then
    ignored, silently swallowing the very failure this fixes.
    """
    if not item_quantities:
        return True, None
    current = fetch_current_available_quantities(domain, token, api_version, location_id, [iid for iid, _ in item_quantities])
    mutation = '''
    mutation($input: InventorySetQuantitiesInput!, $key: String!) {
      inventorySetQuantities(input: $input) @idempotent(key: $key) {
        inventoryAdjustmentGroup { changes { name delta } }
        userErrors { field message }
      }
    }
    '''
    variables = {
        'input': {
            'name': 'available',
            'reason': 'correction',
            'quantities': [
                {'inventoryItemId': iid, 'locationId': location_id, 'quantity': q,
                 'changeFromQuantity': current.get(iid, 0)}
                for iid, q in item_quantities
            ],
        },
        'key': str(uuid.uuid4()),
    }
    response = graphql_request(domain, token, api_version, mutation, variables)
    errs = response.get('errors') or response.get('data', {}).get('inventorySetQuantities', {}).get('userErrors')
    if errs:
        log({'action': 'INVENTORY_SET_FAILED', 'error': str(errs), 'item_count': len(item_quantities)})
        return False, response
    return True, response


def to_int_quantity(value):
    n = to_number(value)
    return int(n) if n is not None else 0


def expected_inventory(manage_stock, stock_quantity):
    if manage_stock != 'yes':
        return None  # untracked - not applicable
    return to_int_quantity(stock_quantity)


def set_simple_variant(domain, token, api_version, product_gid, variant_gid, product):
    reg = to_number(product['regular_price'])
    price = to_number(product['price']) or reg
    if price is None:
        # Should be unreachable: a simple product with no price is
        # quarantined upstream (flag_missing_price, code='no_source_price')
        # and never reaches process_product at all. Fail loud rather than
        # silently fabricate '0.00' if that invariant is ever violated -
        # explicit store-owner rule, Phase 9.7 pricing-safety recovery.
        raise RuntimeError(f"set_simple_variant: product {product['id']} has no price - should have been quarantined upstream, refusing to fabricate 0.00")
    compare = reg if reg and price and reg != price else None
    tracked = product['manage_stock'] == 'yes'
    mutation = '''
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id inventoryItem { id } }
        userErrors { field message }
      }
    }
    '''
    variant_input = {'id': variant_gid, 'price': str(price)}
    if compare:
        variant_input['compareAtPrice'] = str(compare)
    if product['sku'] or tracked:
        variant_input['inventoryItem'] = {'sku': product['sku'], 'tracked': tracked}
    variables = {'productId': product_gid, 'variants': [variant_input]}
    data = graphql_request(domain, token, api_version, mutation, variables)

    if tracked and not data.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors') and not data.get('errors'):
        inv_item_id = data['data']['productVariantsBulkUpdate']['productVariants'][0]['inventoryItem']['id']
        location_id = get_default_location(domain, token, api_version)
        set_inventory_quantities(domain, token, api_version, location_id,
                                  [(inv_item_id, to_int_quantity(product['stock_quantity']))])
    return data


def set_variable_variants(domain, token, api_version, product_gid, product):
    """Only variations that are BOTH option-valid (is_valid_variation) AND
    priced (has_price) get a real Shopify variant. Two independent skip
    reasons are tracked and returned separately so the audit trail names
    the actual cause per variation - never merged into one generic
    'skipped' bucket. Never sends '0.00' for a variation with no source
    price (explicit store-owner rule, Phase 9.7 pricing-safety recovery)."""
    option_name = product['variation_option_names'][0] if product['variation_option_names'] else 'Option'
    option_valid, skipped_option = partition_variations(product['variations'])
    priced_variations, skipped_price = partition_by_price(option_valid)
    values = [{'name': v['options'][0]} for v in priced_variations]
    mutation = '''
    mutation($productId: ID!, $options: [OptionCreateInput!]!) {
      productOptionsCreate(productId: $productId, options: $options, variantStrategy: CREATE) {
        product { id options { id name optionValues { id name } } }
        userErrors { field message }
      }
    }
    '''
    variables = {'productId': product_gid, 'options': [{'name': option_name, 'values': values}]}
    data = graphql_request(domain, token, api_version, mutation, variables)
    errs = data.get('data', {}).get('productOptionsCreate', {}).get('userErrors') or data.get('errors')
    if errs:
        return data, [], skipped_option, skipped_price

    data2 = graphql_request(domain, token, api_version,
                             '{ product(id: "%s") { variants(first: 50) { edges { node { id selectedOptions { name value } } } } } }' % product_gid)
    variant_gids_by_value = {}
    for edge in data2['data']['product']['variants']['edges']:
        for opt in edge['node']['selectedOptions']:
            if opt['name'] == option_name:
                variant_gids_by_value[opt['value']] = edge['node']['id']

    bulk_inputs = []
    tracked_quantities = []  # (index into bulk_inputs, stock_quantity) for variants that will be tracked
    for v in priced_variations:
        value = v['options'][0]
        gid = variant_gids_by_value.get(value)
        if not gid:
            continue
        reg = to_number(v['regular_price'])
        price = to_number(v['price']) or reg
        compare = reg if reg and price and reg != price else None
        tracked = v['manage_stock'] == 'yes'
        vi = {'id': gid, 'price': str(price)}
        if compare:
            vi['compareAtPrice'] = str(compare)
        if v['sku'] or tracked:
            vi['inventoryItem'] = {'sku': v['sku'], 'tracked': tracked}
        if tracked:
            tracked_quantities.append((len(bulk_inputs), v['stock_quantity']))
        bulk_inputs.append(vi)

    mutation2 = '''
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id inventoryItem { id } }
        userErrors { field message }
      }
    }
    '''
    data3 = graphql_request(domain, token, api_version, mutation2, {'productId': product_gid, 'variants': bulk_inputs})

    if tracked_quantities and not data3.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors') and not data3.get('errors'):
        result_variants = data3['data']['productVariantsBulkUpdate']['productVariants']
        item_quantities = [(result_variants[i]['inventoryItem']['id'], to_int_quantity(qty)) for i, qty in tracked_quantities]
        location_id = get_default_location(domain, token, api_version)
        set_inventory_quantities(domain, token, api_version, location_id, item_quantities)

    return data, data3, skipped_option, skipped_price


def expected_variant_count(product):
    if product['wc_type'] == 'variable' and product['variations']:
        option_valid, _ = partition_variations(product['variations'])
        priced, _ = partition_by_price(option_valid)
        return len(priced)
    return 1


def expected_media_count(product):
    return len([u for u in product['images'] if not u.lower().endswith('.avif')])


def live_variants_with_inventory(domain, token, api_version, product_gid):
    data = graphql_request(domain, token, api_version,
                            '{ product(id: "%s") { variants(first: 50) { edges { node { '
                            'id selectedOptions { name value } inventoryItem { id tracked } } } } } }' % product_gid)
    return [e['node'] for e in data['data']['product']['variants']['edges']]


def sync_inventory_for_existing(domain, token, api_version, product_gid, product, live_variants):
    """Applied on the idempotent-update path so already-created products
    (from before this fix existed) end up correct too, not just newly
    created ones. Setting the same absolute quantity twice is a no-op, so
    this is safe to run on every update."""
    def match_live(option_value):
        if option_value is None:
            return live_variants[0] if live_variants else None
        for lv in live_variants:
            for opt in lv['selectedOptions']:
                if opt['value'] == option_value:
                    return lv
        return None

    pairs = []  # (live_variant_node, expected_quantity)
    if product['wc_type'] == 'variable' and product['variations']:
        option_valid, _skipped = partition_variations(product['variations'])
        priced_variations, _skipped_price = partition_by_price(option_valid)
        for v in priced_variations:
            lv = match_live(v['options'][0])
            qty = expected_inventory(v['manage_stock'], v['stock_quantity'])
            if lv and qty is not None:
                pairs.append((lv, qty))
    else:
        lv = live_variants[0] if live_variants else None
        qty = expected_inventory(product['manage_stock'], product['stock_quantity'])
        if lv and qty is not None:
            pairs.append((lv, qty))

    to_track = []
    item_quantities = []

    for lv, qty in pairs:
        if qty is None:
            continue
        if not lv['inventoryItem']['tracked']:
            to_track.append(lv['inventoryItem']['id'])
        item_quantities.append((lv['inventoryItem']['id'], qty))

    for iid in to_track:
        graphql_request(domain, token, api_version, '''
        mutation($id: ID!) { inventoryItemUpdate(id: $id, input: { tracked: true }) { userErrors { field message } } }
        ''', {'id': iid})
    if not item_quantities:
        return 0, True
    location_id = get_default_location(domain, token, api_version)
    success, _ = set_inventory_quantities(domain, token, api_version, location_id, item_quantities)
    return len(item_quantities), success


def create_or_fix_variants(domain, token, api_version, product_gid, product):
    """Runs the full variant/option/inventory setup. Safe to call on a
    product that still has only Shopify's auto-created default variant
    (a prior run's variant step never having succeeded) - NOT safe to call
    on a product whose real variants already exist, since productOptionsCreate
    would then try to add a second, conflicting option. Callers gate this."""
    variant_errors = []
    variants_done = 0
    skipped_option = []
    skipped_price = []
    if product['wc_type'] == 'variable' and product['variations']:
        opt_data, bulk_data, skipped_option, skipped_price = set_variable_variants(domain, token, api_version, product_gid, product)
        opt_errs = opt_data.get('data', {}).get('productOptionsCreate', {}).get('userErrors') or opt_data.get('errors')
        if opt_errs:
            variant_errors.append(('productOptionsCreate', opt_errs))
        elif bulk_data:
            bulk_errs = bulk_data.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors') or bulk_data.get('errors')
            if bulk_errs:
                variant_errors.append(('productVariantsBulkUpdate', bulk_errs))
            else:
                option_valid, _ = partition_variations(product['variations'])
                priced, _ = partition_by_price(option_valid)
                variants_done = len(priced)
    else:
        variant_gid = get_default_variant(domain, token, api_version, product_gid)
        vdata = set_simple_variant(domain, token, api_version, product_gid, variant_gid, product)
        verrs = vdata.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors') or vdata.get('errors')
        if verrs:
            variant_errors.append(('productVariantsBulkUpdate', verrs))
        else:
            variants_done = 1
    return variant_errors, variants_done, skipped_option, skipped_price


def upload_media(domain, token, api_version, product_gid, product):
    images = [u for u in product['images'] if not u.lower().endswith('.avif')]
    skipped = [u for u in product['images'] if u.lower().endswith('.avif')]
    if not images:
        return None, skipped
    media = [{'originalSource': url, 'alt': product['title'], 'mediaContentType': 'IMAGE'} for url in images]
    mutation = '''
    mutation($productId: ID!, $media: [CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
        media { id }
        mediaUserErrors { field message }
      }
    }
    '''
    data = graphql_request(domain, token, api_version, mutation, {'productId': product_gid, 'media': media})
    return data, skipped


def process_product(domain, token, api_version, woo_id, product, existing_gid, run_id, commit):
    """Shared CREATE/UPDATE/variant-retry/inventory-sync/media-retry logic
    for one non-quarantined product. Used by both this script's own main()
    (the original 9-product test path) and phase9_bulk_import.py, so a fix
    made here applies to both instead of two copies silently drifting
    apart. Returns (record, skipped_variation_records).

    Media-retry addition (Phase 9.7 Step 5 recovery): a product whose
    earlier run created the base product but crashed before media upload
    (exactly what happened to products 18/16464 in the pilot batch crash)
    would otherwise never receive its media, since media was previously
    only ever uploaded on the CREATE path. Same actual<expected pattern
    already used for variants, applied here to media too."""
    record = {'woo_id': woo_id, 'title': product['title']}
    skipped_records = []

    if existing_gid:
        data = update_product(domain, token, api_version, existing_gid, product)
        errs = data.get('data', {}).get('productUpdate', {}).get('userErrors') or data.get('errors')
        if errs:
            record.update(action='FAILED', shopify_gid=existing_gid, error=str(errs))
            log({'woo_id': woo_id, 'action': 'FAILED', 'error': str(errs)})
            return record, skipped_records

        expected = expected_variant_count(product)
        live_variants = live_variants_with_inventory(domain, token, api_version, existing_gid)
        actual = len(live_variants)
        variant_errors = None
        if actual < expected:
            # A prior run's variant/option step never completed (e.g.
            # product 1721's productOptionsCreate failure, or products
            # 18/16464's IndexError crash) - the product still has only
            # Shopify's single auto-created default variant, so it's safe
            # to run variant setup now. Does NOT trigger when
            # actual == expected (already complete) or actual > expected.
            variant_errors, variants_done, skipped_option, skipped_price = create_or_fix_variants(domain, token, api_version, existing_gid, product)
            skipped_records = (
                [build_skipped_variation_record(product, v, run_id, commit, 'BROKEN_VARIATION_SKIPPED') for v in skipped_option]
                + [build_skipped_variation_record(product, v, run_id, commit, 'MISSING_PRICE_SKIPPED', 'NO_SOURCE_PRICE') for v in skipped_price]
            )
            if variant_errors:
                log({'woo_id': woo_id, 'action': 'VARIANT_RETRY_FAILED', 'error': str(variant_errors)})
            else:
                log({'woo_id': woo_id, 'action': 'VARIANT_RETRY_SUCCEEDED', 'variants': variants_done})
                if skipped_option:
                    log({'woo_id': woo_id, 'action': 'BROKEN_VARIATION_SKIPPED', 'variation_ids': [v['id'] for v in skipped_option]})
                if skipped_price:
                    log({'woo_id': woo_id, 'action': 'MISSING_PRICE_SKIPPED', 'variation_ids': [v['id'] for v in skipped_price]})
                live_variants = live_variants_with_inventory(domain, token, api_version, existing_gid)
        else:
            # Already complete - safe to (re-)sync inventory quantities,
            # since setting the same absolute value twice is a no-op.
            attempted, inv_success = sync_inventory_for_existing(domain, token, api_version, existing_gid, product, live_variants)
            if attempted:
                log({'woo_id': woo_id, 'action': 'INVENTORY_SYNCED' if inv_success else 'INVENTORY_SYNC_FAILED', 'variants': attempted})

        expected_media = expected_media_count(product)
        if expected_media:
            live_media = graphql_request(domain, token, api_version,
                                          '{ product(id: "%s") { media(first: 50) { edges { node { id } } } } }' % existing_gid)
            actual_media = len(live_media.get('data', {}).get('product', {}).get('media', {}).get('edges', []))
            if actual_media < expected_media:
                media_data, _skipped_avif = upload_media(domain, token, api_version, existing_gid, product)
                merrs = (media_data.get('data', {}).get('productCreateMedia', {}).get('mediaUserErrors') or media_data.get('errors')) if media_data else None
                log({'woo_id': woo_id, 'action': 'MEDIA_RETRY_FAILED' if merrs else 'MEDIA_RETRY_SUCCEEDED', 'error': str(merrs) if merrs else None})

        record.update(action='UPDATED', shopify_gid=existing_gid,
                       note='top-level fields only; variants/media retried only because a prior run left them incomplete' if actual < expected
                       else 'top-level fields, inventory synced; variants/media not re-run unless a gap was detected, by design',
                       variant_retry_errors=variant_errors,
                       skipped_variation_ids=[r['woo_variation_id'] for r in skipped_records] or None)
        checkpoint(woo_id, existing_gid, 'UPDATED')
        log({'woo_id': woo_id, 'action': 'UPDATED', 'shopify_gid': existing_gid})
        return record, skipped_records

    data = create_product(domain, token, api_version, product)
    errs = data.get('data', {}).get('productCreate', {}).get('userErrors') or data.get('errors')
    if errs or not data.get('data', {}).get('productCreate', {}).get('product'):
        record.update(action='FAILED', shopify_gid=None, error=str(errs or data))
        log({'woo_id': woo_id, 'action': 'FAILED', 'error': str(errs or data)})
        return record, skipped_records

    product_gid = data['data']['productCreate']['product']['id']
    checkpoint(woo_id, product_gid, 'CREATED')
    log({'woo_id': woo_id, 'action': 'CREATED', 'shopify_gid': product_gid})

    variant_errors, variants_done, skipped_option, skipped_price = create_or_fix_variants(domain, token, api_version, product_gid, product)
    skipped_records = (
        [build_skipped_variation_record(product, v, run_id, commit, 'BROKEN_VARIATION_SKIPPED') for v in skipped_option]
        + [build_skipped_variation_record(product, v, run_id, commit, 'MISSING_PRICE_SKIPPED', 'NO_SOURCE_PRICE') for v in skipped_price]
    )
    if skipped_option:
        log({'woo_id': woo_id, 'action': 'BROKEN_VARIATION_SKIPPED', 'variation_ids': [v['id'] for v in skipped_option]})
    if skipped_price:
        log({'woo_id': woo_id, 'action': 'MISSING_PRICE_SKIPPED', 'variation_ids': [v['id'] for v in skipped_price]})

    media_data, skipped_avif = upload_media(domain, token, api_version, product_gid, product)
    media_errors = []
    if media_data:
        merrs = media_data.get('data', {}).get('productCreateMedia', {}).get('mediaUserErrors') or media_data.get('errors')
        if merrs:
            media_errors.append(merrs)

    intended_collections = resolve_collections(product, build_collection_lookup(load_collections()))

    record.update(
        action='CREATED', shopify_gid=product_gid,
        variant_errors=variant_errors or None,
        media_errors=media_errors or None,
        avif_images_skipped=skipped_avif,
        skipped_variation_ids=[r['woo_variation_id'] for r in skipped_records] or None,
        variants_done=variants_done,
        intended_collections_not_assigned=intended_collections,
    )
    if variant_errors:
        log({'woo_id': woo_id, 'action': 'VARIANT_ERROR', 'error': str(variant_errors)})
    if media_errors:
        log({'woo_id': woo_id, 'action': 'MEDIA_ERROR', 'error': str(media_errors)})
    return record, skipped_records


def main():
    config = get_config()
    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'

    if not domain or not token:
        print('NOT_CONFIGURED - cannot proceed.')
        return 2

    print('=== Final pre-write preflight ===')
    ok, findings = final_preflight(domain, token, api_version, config['environment'])
    for name, status, detail in findings:
        print(f'[{name}] {status} - {detail}')
    if not ok:
        print('\nSTOP: one or more pre-write checks failed. No write attempted.')
        return 1
    print('\nAll pre-write checks passed. Proceeding to write.\n')

    os.makedirs(REPORTS_DIR, exist_ok=True)
    products = load_products()
    by_id = {p['id']: p for p in products}
    dq_issues = run_data_quality(products) + flag_garbage_brand_products(products) + flag_missing_price(products)
    blocking_or_stop = {
        i['woo_id'] for i in dq_issues
        if i['severity'] == 'BLOCKING' or i.get('code') in ('ambiguous_vendor', 'no_source_price')
    }

    run_id = new_run_id()
    commit = get_importer_commit()
    ids = read_test_set_ids()
    result = {'attempted': 0, 'created': 0, 'updated': 0, 'quarantined': 0, 'failed': 0,
              'variants_created_or_updated': 0, 'media_created': 0, 'media_skipped_avif': 0,
              'variations_skipped': 0, 'records': []}

    for woo_id in ids:
        product = by_id[woo_id]
        result['attempted'] += 1

        if woo_id in blocking_or_stop:
            reason = '; '.join(i['detail'] for i in dq_issues if i['woo_id'] == woo_id and (i['severity'] == 'BLOCKING' or i.get('code') in ('ambiguous_vendor', 'no_source_price')))
            record = {'woo_id': woo_id, 'title': product['title'], 'action': 'QUARANTINED', 'shopify_gid': None, 'reason': reason}
            result['quarantined'] += 1
            log({'woo_id': woo_id, 'action': 'QUARANTINED', 'reason': reason})
            result['records'].append(record)
            continue

        try:
            existing_gid = find_existing_by_legacy_id(domain, token, api_version, woo_id)
        except Exception as e:
            record = {'woo_id': woo_id, 'title': product['title'], 'action': 'FAILED', 'shopify_gid': None, 'error': f'lookup failed: {e}'}
            result['failed'] += 1
            log({'woo_id': woo_id, 'action': 'FAILED', 'error': str(e)})
            result['records'].append(record)
            continue

        record, skipped_records = process_product(domain, token, api_version, woo_id, product, existing_gid, run_id, commit)
        log_skipped_variations(skipped_records)
        result['variations_skipped'] += len(skipped_records)
        if record['action'] == 'CREATED':
            result['created'] += 1
            result['variants_created_or_updated'] += record.get('variants_done', 0)
            if not record.get('media_errors'):
                result['media_created'] += len(product['images']) - len(record.get('avif_images_skipped') or [])
            result['media_skipped_avif'] += len(record.get('avif_images_skipped') or [])
        elif record['action'] == 'UPDATED':
            result['updated'] += 1
        else:
            result['failed'] += 1
        result['records'].append(record)

    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)

    print(json.dumps({k: v for k, v in result.items() if k != 'records'}, indent=2))
    print(f'\nWrote {RESULT_PATH}, {LOG_PATH}, {CHECKPOINT_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
