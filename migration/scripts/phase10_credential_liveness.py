"""Measure the actual lifetime of the Shopify credential. READ-ONLY.

Issues one trivial query (`{ shop { name } }`, cost 1) and records whether it
succeeded, with a timestamp and a token fingerprint. Never writes to Shopify,
never prints or logs a token value.

WHY THIS EXISTS
---------------
The credential has been replaced twice in under two weeks, and the repository
cannot tell us why. Two explanations fit the evidence equally well and imply
completely different operational procedures:

  (a) The token EXPIRES on a clock (online tokens last 24 hours or until the
      staff user logs out of the Shopify admin). If so, the migration must be
      started on a freshly minted token and cannot be paused overnight.

  (b) The token is INVALIDATED BY REINSTALLATION - a new install, a scope
      change, or a `shopify app dev` session retires the previous token. If so
      the token is stable indefinitely, and the fix is procedural: do not touch
      the app while a migration is running.

Nothing in this repository distinguishes them, and guessing would produce a
confident procedure built on the wrong model. Probing does distinguish them:
under (a) the token dies unattended on a predictable schedule; under (b) it
survives until somebody acts on the app.

HOW TO USE
----------
Run it repeatedly - a scheduled task every 30-60 minutes is ideal, but running
it by hand a few times a day also works. Each run appends one line. After a day
or two the ledger answers the question directly, with no access to the Shopify
Admin or Partner Dashboard required:

  * token dies with nobody touching the app  -> clock expiry, model (a)
  * token survives days, dies only after an
    app action, or fingerprint changes       -> reinstall, model (b)

    python migration/scripts/phase10_credential_liveness.py

Writes reports/phase10_credential_liveness.jsonl (TRACKED - fingerprints and
status only, no token value, no PII).
"""
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase9_preflight import get_config, graphql_request

LEDGER_PATH = os.path.join('reports', 'phase10_credential_liveness.jsonl')

PROBE = '{ shop { name } }'


def fingerprint(token):
    """Stable, non-reversible label so a token CHANGE is visible in the ledger
    without the value ever being recorded."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]


def classify(exc):
    text = f'{type(exc).__name__}: {exc}'
    if '401' in text or 'Unauthorized' in text or 'Invalid API key' in text:
        return 'DEAD_401', 'credential no longer recognised by Shopify'
    if '403' in text:
        return 'FORBIDDEN_403', 'authenticated but refused'
    if '429' in text or 'Throttl' in text:
        return 'THROTTLED', 'rate limited - not a credential problem'
    return 'ERROR', 'transport or unexpected error'


def main():
    config = get_config()
    domain, token = config['domain'], config['token']
    api_version = config['api_version'] or '2025-01'
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if not domain or not token:
        entry = {'ts': now, 'status': 'NOT_CONFIGURED', 'token_fingerprint': None,
                 'token_prefix': None, 'detail': 'no credential present'}
    else:
        record = {'ts': now, 'token_fingerprint': fingerprint(token),
                  'token_prefix': token[:6], 'token_length': len(token),
                  'domain': domain, 'api_version': api_version}
        try:
            data = graphql_request(domain, token, api_version, PROBE)
            if 'errors' in data:
                codes = {(e.get('extensions') or {}).get('code') for e in data['errors']}
                record.update(status='GRAPHQL_ERROR', detail=str(sorted(c for c in codes if c)))
            else:
                cost = (data.get('extensions') or {}).get('cost') or {}
                throttle = cost.get('throttleStatus') or {}
                record.update(status='ALIVE', detail='ok',
                              throttle_available=throttle.get('currentlyAvailable'))
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised
            status, detail = classify(exc)
            record.update(status=status, detail=detail)
        entry = record

    os.makedirs('reports', exist_ok=True)
    with open(LEDGER_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())

    print(f"{entry['ts']}  {entry['status']:<14} "
          f"fingerprint={entry.get('token_fingerprint')}  {entry.get('detail', '')}")

    # Summarise the ledger so the answer is visible without reading JSONL.
    try:
        rows = [json.loads(line) for line in open(LEDGER_PATH, encoding='utf-8') if line.strip()]
    except Exception:  # noqa: BLE001
        return 0
    if len(rows) < 2:
        print('  (one observation so far - run this repeatedly to measure lifetime)')
        return 0

    prints = [r.get('token_fingerprint') for r in rows if r.get('token_fingerprint')]
    changes = sum(1 for a, b in zip(prints, prints[1:]) if a != b)
    alive = [r for r in rows if r.get('status') == 'ALIVE']
    dead = [r for r in rows if r.get('status') == 'DEAD_401']
    print(f'  ledger: {len(rows)} observation(s), {len(alive)} alive, {len(dead)} dead-401, '
          f'{changes} credential change(s)')
    if alive:
        span_start, span_end = alive[0]['ts'], alive[-1]['ts']
        same = alive[0].get('token_fingerprint') == alive[-1].get('token_fingerprint')
        print(f'  earliest alive {span_start}, latest alive {span_end}'
              f"{' (same credential throughout)' if same else ' (credential changed in between)'}")
    if dead and not changes:
        print('  A credential died with no fingerprint change: consistent with CLOCK '
              'EXPIRY, not reinstallation.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
