"""
Phase 9 Admin API pre-flight check. Read-only - performs zero mutations
against Shopify, ever. Verifies the store/credentials/permissions this
project would need before any real import, without importing anything.

No credentials are hardcoded here, none are logged, none are written to
any report this script produces. If SHOPIFY_STORE_DOMAIN or
SHOPIFY_ADMIN_API_ACCESS_TOKEN aren't set, this exits cleanly with
NOT_CONFIGURED - it does not fabricate a passing result.

Usage:
    python migration/scripts/phase9_preflight.py

Reads configuration from environment variables (see .env.example) or a
local .env file in the repo root (never committed - see .gitignore).
"""
import json
import os
import sys
import urllib.error
import urllib.request

REPORTS_DIR = 'reports'
REQUIRED_SCOPES = {
    'read_products', 'write_products',
    'read_product_listings',
    'read_inventory', 'write_inventory',
    'read_metaobjects', 'write_metaobjects',
    'read_files', 'write_files',
}


def load_dotenv(path='.env'):
    """Minimal .env loader - stdlib only, no new dependency. Does not log
    or print any value it reads."""
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_config():
    load_dotenv()
    return {
        'environment': os.environ.get('SHOPIFY_ENVIRONMENT', ''),
        'domain': os.environ.get('SHOPIFY_STORE_DOMAIN', ''),
        'token': os.environ.get('SHOPIFY_ADMIN_API_ACCESS_TOKEN', ''),
        'api_version': os.environ.get('SHOPIFY_API_VERSION', ''),
    }


def graphql_request(domain, token, api_version, query, variables=None):
    url = f'https://{domain}/admin/api/{api_version}/graphql.json'
    payload = json.dumps({'query': query, 'variables': variables or {}}).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload, method='POST',
        headers={
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': token,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def check(name, fn, results):
    print(f'[{name}] running...')
    try:
        detail = fn()
        results.append({'check': name, 'status': 'PASS', 'detail': detail})
        print(f'[{name}] PASS - {detail}')
        return True
    except Exception as e:
        results.append({'check': name, 'status': 'FAIL', 'detail': str(e)})
        print(f'[{name}] FAIL - {e}')
        return False


def main():
    config = get_config()
    results = []

    if not config['domain'] or not config['token']:
        print('NOT_CONFIGURED: SHOPIFY_STORE_DOMAIN and/or SHOPIFY_ADMIN_API_ACCESS_TOKEN '
              'are not set (checked environment and .env). This is the expected, honest '
              'result in this environment - no Shopify store or credentials have been '
              'provisioned for this project yet. See docs/PHASE9_ENVIRONMENT_READINESS.md.')
        write_report([{'check': 'configuration', 'status': 'NOT_CONFIGURED',
                        'detail': 'Required environment variables not set'}])
        return 2

    domain, token, api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
    print(f"Pre-flight against store: {domain} (environment={config['environment'] or 'unspecified'}, "
          f"API version={api_version})")
    print('This script performs READ-ONLY queries only. No product, collection, '
          'or metafield will be created, updated, or deleted.')
    print()

    def auth_and_identity():
        data = graphql_request(domain, token, api_version, '{ shop { name myshopifyDomain plan { displayName } } }')
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        shop = data['data']['shop']
        return f"authenticated as shop '{shop['name']}' ({shop['myshopifyDomain']}), plan: {shop['plan']['displayName']}"

    def scopes():
        data = graphql_request(domain, token, api_version,
                                '{ currentAppInstallation { accessScopes { handle } } }')
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        granted = {s['handle'] for s in data['data']['currentAppInstallation']['accessScopes']}
        missing = REQUIRED_SCOPES - granted
        if missing:
            raise RuntimeError(f'missing required scopes: {sorted(missing)}')
        return f'all {len(REQUIRED_SCOPES)} required scopes granted'

    def read_products():
        data = graphql_request(domain, token, api_version, '{ products(first: 1) { edges { node { id title } } } }')
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        return f"read succeeded ({len(data['data']['products']['edges'])} product(s) returned)"

    def read_collections():
        data = graphql_request(domain, token, api_version, '{ collections(first: 1) { edges { node { id title } } } }')
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        return f"read succeeded ({len(data['data']['collections']['edges'])} collection(s) returned)"

    def read_metaobject_definitions():
        data = graphql_request(domain, token, api_version,
                                '{ metaobjectDefinitions(first: 5) { edges { node { type } } } }')
        if 'errors' in data:
            raise RuntimeError(str(data['errors']))
        types = [e['node']['type'] for e in data['data']['metaobjectDefinitions']['edges']]
        return f"read succeeded, existing metaobject types: {types or 'none defined yet'}"

    all_pass = True
    all_pass &= check('authentication_and_store_identity', auth_and_identity, results)
    all_pass &= check('required_scopes_granted', scopes, results)
    all_pass &= check('read_products', read_products, results)
    all_pass &= check('read_collections', read_collections, results)
    all_pass &= check('read_metaobject_definitions', read_metaobject_definitions, results)

    print()
    print('NOT performed (write operations - explicitly out of scope for pre-flight):')
    print('  - create/update product, collection, or metafield definition')
    print('  - any mutation of any kind')

    write_report(results)
    return 0 if all_pass else 1


def write_report(results):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, 'phase9_preflight_result.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({
            'note': 'Read-only pre-flight check result. Contains no credentials, tokens, or PII.',
            'checks': results,
        }, f, indent=2)
    print(f'\nWrote {path}')


if __name__ == '__main__':
    sys.exit(main())
