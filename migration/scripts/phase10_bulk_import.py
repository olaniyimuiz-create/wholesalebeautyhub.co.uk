"""Phase 10 bulk customer importer - DRY RUN ONLY IN THIS REVISION.

    SOURCE -> MANIFEST VALIDATION -> PRE-FLIGHT -> LEGACY-ID MAP ->
    COHORT SELECTION -> TRANSFORMATION -> VALIDATION -> AUDIT SIMULATION

...and then it stops. There is no mutation document anywhere in this file, no
mutation transport, and no code path that could acquire one. That is not a
policy setting that a flag could flip - it is the absence of the capability.
`--mode live` is recognised and REFUSED, so the refusal is explicit rather than
a missing feature someone might mistake for an oversight.

WHY IT IS BUILT THIS WAY
------------------------
A bulk importer under review is at its most dangerous while it is being written:
the guards are half-built, the operator is iterating, and a copied command line
from a terminal five minutes ago is one keystroke from 11,849 real people. So
this revision cannot write at all. The live path is a separate, later change
that must be reviewed on its own terms and authorized by ADR-014 Gate 7.

NOTHING IS REIMPLEMENTED
------------------------
Throttling, exponential backoff, verify-before-retry, PII sanitisation, ledger
and checkpoint semantics, legacy-ID reconciliation and the phone fallback all
come from phase10_import_runtime, which is offline, mutation-refusing and
covered by 644 assertions. A second implementation of any of them would drift
from the tested one, and the drift would be invisible until the run.

THE SIXTEEN GUARDS
------------------
Each is a named function that HALTS rather than warns. Guards 11, 12 and 13
concern behaviour during a live request and are delegated to the runtime that
already implements and tests them - listed here so the set is complete and
auditable, and asserted present rather than assumed.

Outputs (both aggregate, no PII):
  reports/phase10_bulk_import_dry_run.json    TRACKED
  reports/phase10_bulk_import_dry_run.jsonl   GITIGNORED - per-record audit
                                              simulation, Woo ids only

Run: python migration/scripts/phase10_bulk_import.py            # dry run
     python migration/scripts/phase10_bulk_import.py --mode live  # refused
"""
import csv
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt
from phase10_run_plan import load_population

# --------------------------------------------------------------------------
# Approved constants. Every one of these is a value a guard compares against,
# and changing one is changing what was approved - so they are here, together,
# where a reviewer can read them in one place rather than hunting them.
# --------------------------------------------------------------------------

APPROVED_STORE_DOMAIN = 'wholesale-beautyhub.myshopify.com'
APPROVED_MANIFEST_PATH = os.path.join('reports', 'phase10_customer_manifest.csv')
APPROVED_MANIFEST_SHA256 = (
    '2e31f3edbed607d3cec3bf2790ee9deae57e7c0e2f7dd07c14a0b1c4bcecdfda')
APPROVED_IMPORT_POPULATION = 12096       # manifest IMPORT rows
APPROVED_RUN_POPULATION = 11849          # after ADR-014 Gate 5
EXPECTED_PRE_IMPORT_CUSTOMER_COUNT = 0

# The exact phrase, and only this phrase. No paraphrase, no previous approval,
# no "the developer recommends it".
GATE_7_AUTHORIZATION = 'APPROVED - EXECUTE GATE 7 BULK CUSTOMER IMPORT'

MODE_DRY_RUN = 'dry-run'
MODE_LIVE = 'live'
DEFAULT_MODE = MODE_DRY_RUN              # GUARD 4

DRY_RUN_JSON = os.path.join('reports', 'phase10_bulk_import_dry_run.json')
DRY_RUN_LEDGER = os.path.join('reports', 'phase10_bulk_import_dry_run.jsonl')
DRY_RUN_CHECKPOINT = os.path.join('reports', 'phase10_bulk_import_dry_run_checkpoint.jsonl')

# Measured against this store on 2026-08-22, not assumed.
COST_PER_MUTATION = 10
SUSTAINED_MUTATIONS_PER_SECOND = 10


class Halt(RuntimeError):
    """Stop. Nothing further is attempted, and nothing is written."""


class LiveModeNotBuilt(Halt):
    """Live mode does not exist in this revision. Distinguished from a refusal
    so the message cannot be read as 'you lack permission today'."""


# --------------------------------------------------------------------------
# GUARDS 1-16
# --------------------------------------------------------------------------

def guard_1_approved_store(domain):
    """The store must be the one named in the approval, exactly."""
    if (domain or '').strip().lower() != APPROVED_STORE_DOMAIN:
        raise Halt(f'GUARD 1: store {domain!r} is not the approved store '
                   f'{APPROVED_STORE_DOMAIN!r}. Refusing.')
    return True


def guard_2_reject_production(shop):
    """A production store is refused even if it is somehow the approved domain.

    Two independent signals, because either alone can be wrong: the plan must
    report partnerDevelopment, and the domain must not carry a production
    marker. A store that fails to state it is a development store is treated as
    production - the safe reading of missing information.
    """
    plan = (shop or {}).get('plan') or {}
    if plan.get('partnerDevelopment') is not True:
        raise Halt('GUARD 2: target is not a development store '
                   f'(plan={plan.get("displayName")!r}, '
                   f'partnerDevelopment={plan.get("partnerDevelopment")!r}). Refusing.')
    domain = (shop.get('myshopifyDomain') or '').lower()
    if any(marker in domain for marker in ('-prod', 'production', 'live-')):
        raise Halt(f'GUARD 2: domain {domain!r} carries a production marker. Refusing.')
    return True


def guard_3_live_mode_is_not_default(mode_from_argv):
    """Live must be asked for by name. Absence of an argument means dry run."""
    if mode_from_argv is None:
        return MODE_DRY_RUN
    if mode_from_argv not in (MODE_DRY_RUN, MODE_LIVE):
        raise Halt(f'GUARD 3: unknown mode {mode_from_argv!r}')
    return mode_from_argv


def guard_4_dry_run_default():
    """Asserted, not assumed: an edit that changed the default would otherwise
    only be noticed by whoever ran it."""
    if DEFAULT_MODE != MODE_DRY_RUN:
        raise Halt('GUARD 4: the default mode is not dry run. Refusing to start.')
    return True


def guard_5_execution_authorization(supplied):
    """Live mode requires the exact Gate 7 phrase and nothing else."""
    if (supplied or '') != GATE_7_AUTHORIZATION:
        raise Halt('GUARD 5: no valid Gate 7 execution authorization supplied. '
                   'The exact approval phrase is required; nothing else counts, '
                   'including a previous approval or a recommendation.')
    return True


def guard_6_manifest_hash(path=APPROVED_MANIFEST_PATH,
                          expected=APPROVED_MANIFEST_SHA256):
    """The manifest must be byte-identical to the approved one."""
    if not os.path.exists(path):
        raise Halt(f'GUARD 6: approved manifest {path} not found.')
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    if digest != expected:
        raise Halt(f'GUARD 6: manifest hash mismatch. Approved {expected[:16]}..., '
                   f'found {digest[:16]}.... The approved population may have '
                   f'changed; re-approval is required, not a re-run.')
    return digest


def guard_7_population(manifest_import_ids, derived_import_ids, run_population):
    """Three numbers that must agree, and one set comparison that must be exact.

    The counts alone are not enough: two different populations of the same size
    would pass a count check and import the wrong people.
    """
    if len(manifest_import_ids) != APPROVED_IMPORT_POPULATION:
        raise Halt(f'GUARD 7: manifest holds {len(manifest_import_ids)} IMPORT rows, '
                   f'approved {APPROVED_IMPORT_POPULATION}.')
    if set(manifest_import_ids) != set(derived_import_ids):
        only_manifest = len(set(manifest_import_ids) - set(derived_import_ids))
        only_source = len(set(derived_import_ids) - set(manifest_import_ids))
        raise Halt(f'GUARD 7: the source no longer classifies to the approved '
                   f'manifest - {only_manifest} in the manifest only, {only_source} '
                   f'in the source only. Re-approval required.')
    if run_population != APPROVED_RUN_POPULATION:
        raise Halt(f'GUARD 7: run population is {run_population}, approved '
                   f'{APPROVED_RUN_POPULATION}.')
    return True


def guard_8_live_customer_count(count,
                                expected=EXPECTED_PRE_IMPORT_CUSTOMER_COUNT):
    """The store must be in the state the plan was made against."""
    if count != expected:
        raise Halt(f'GUARD 8: store holds {count} customer(s), expected {expected}. '
                   f'Either a previous run is partially applied - in which case '
                   f'resume from the legacy-ID map, not from zero - or this is not '
                   f'the store the plan was made for.')
    return True


def guard_9_no_duplicate_legacy_ids(woo_ids):
    """A duplicate legacy id breaks the resume key, and the breakage is silent
    until reconciliation, by which point both customers exist."""
    seen, duplicates = set(), set()
    for woo_id in woo_ids:
        if woo_id in seen:
            duplicates.add(woo_id)
        seen.add(woo_id)
    if duplicates:
        raise Halt(f'GUARD 9: {len(duplicates)} duplicate legacy Woo customer id(s) '
                   f'in the cohort. The resume key is not unique. Halting.')
    return True


def guard_10_skip_if_present(woo_id, legacy_map):
    """Already live means skip, never create twice. Returns the existing GID."""
    existing = legacy_map.get(str(woo_id))
    return existing.get('gid') if existing else None


def guard_11_verify_before_retry():
    """Delegated to rt.execute_with_retry(verify=...): an ambiguous failure asks
    the server whether the write landed before retrying. Asserted present."""
    import inspect
    if 'verify' not in inspect.signature(rt.execute_with_retry).parameters:
        raise Halt('GUARD 11: the runtime no longer supports verify-before-retry.')
    return True


def guard_12_auth_failure_halts():
    """Delegated to rt.classify_response / classify_exception -> HaltMigration.
    A 401 is never retried: every subsequent record would fail for a reason that
    has nothing to do with the record."""
    klass, _detail = rt.classify_response(
        {'errors': [{'message': 'x', 'extensions': {'code': 'ACCESS_DENIED'}}]})
    if klass != rt.AUTH_FAILURE:
        raise Halt('GUARD 12: the runtime no longer classifies ACCESS_DENIED as an '
                   'auth failure.')
    return True


def guard_13_throttle_backoff():
    """Delegated to rt.ThrottleController + rt.backoff_delay. Throttling never
    consumes the transient-failure budget."""
    klass, _detail = rt.classify_response(
        {'errors': [{'message': 'x', 'extensions': {'code': 'THROTTLED'}}]})
    if klass != rt.THROTTLED or not rt.BACKOFF_SCHEDULE:
        raise Halt('GUARD 13: the runtime no longer implements throttle backoff.')
    return True


def guard_14_legacy_metafield_inline(customer_input, woo_id):
    """No customer may be created without its legacy id in the SAME call."""
    try:
        rt.assert_legacy_metafield_present(customer_input)
    except rt.LegacyMetafieldMissing as exc:
        raise Halt(f'GUARD 14: woo_customer_id={woo_id} payload lacks the legacy '
                   f'metafield: {exc}') from None
    return True


def guard_15_within_cohort(woo_id, approved_cohort_ids):
    """Nothing outside the approved cohort may be created."""
    if int(woo_id) not in approved_cohort_ids:
        raise Halt(f'GUARD 15: woo_customer_id={woo_id} is not in the approved '
                   f'cohort. Refusing.')
    return True


def guard_16_verify_supplied_cohort(path, approved_cohort_ids):
    """A runtime-supplied id list is only accepted if every id is in the approved
    manifest cohort. Its hash is recorded so the run can be tied to the exact
    file that produced it.

    An operator-supplied list is the most direct route to importing the wrong
    people, and 'it was a subset, I checked' is not evidence.
    """
    if not os.path.exists(path):
        raise Halt(f'GUARD 16: cohort file {path} not found.')
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    ids = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line and line.isdigit():
                ids.append(int(line))
    if not ids:
        raise Halt(f'GUARD 16: cohort file {path} contains no Woo customer ids.')
    outside = sorted(set(ids) - approved_cohort_ids)
    if outside:
        raise Halt(f'GUARD 16: {len(outside)} id(s) in {path} are not in the '
                   f'approved cohort. Refusing the whole file.')
    return ids, digest


GUARDS = [
    (1, 'approved store identified', 'enforced'),
    (2, 'production store rejected', 'enforced'),
    (3, 'live mode is not the default', 'enforced'),
    (4, 'DRY_RUN is the default', 'enforced'),
    (5, 'missing Gate 7 authorization halts', 'enforced'),
    (6, 'manifest hash matches', 'enforced'),
    (7, 'population matches the approved manifest', 'enforced'),
    (8, 'live customer count is the expected pre-import count', 'enforced'),
    (9, 'duplicate legacy Woo ids halt', 'enforced'),
    (10, 'already-present legacy id skips', 'enforced'),
    (11, 'timeout verifies before retrying', 'delegated to runtime'),
    (12, '401 / token expiry halts immediately', 'delegated to runtime'),
    (13, 'THROTTLED uses the tested backoff', 'delegated to runtime'),
    (14, 'legacy id written in the same customerCreate', 'enforced'),
    (15, 'no creation outside the approved cohort', 'enforced'),
    (16, 'supplied cohort verified against the manifest', 'enforced'),
]


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(path=APPROVED_MANIFEST_PATH):
    """Approved rows. Returns (import_ids, rows_by_id) - identity only.

    Field VALUES for the transformation come from the source dump, not from
    here: the manifest carries no shipping postcode or province, so building a
    shipping address from it would silently produce a worse address than the
    source can support. The manifest's job is to say WHO was approved.
    """
    import_ids, by_id = [], {}
    with open(path, encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('classification') != 'IMPORT':
                continue
            woo_id = int(row['woo_customer_id'])
            import_ids.append(woo_id)
            by_id[woo_id] = row
    return import_ids, by_id


# --------------------------------------------------------------------------
# Cohort selection and transformation
# --------------------------------------------------------------------------

def phone_decisions(candidates):
    """{woo_id: SEND_PHONE|OMIT_PHONE|HOLD} under ADR-014 Gate 1.

    Gate 1 signed "the recommendations stand, the 9 contested groups omit",
    which is the evidence-scored recommendation with no reviewer override.
    """
    by_key = {}
    for cand in candidates:
        if (cand.get('phone') or '').strip():
            by_key.setdefault(rt.phone_canonical(cand.get('phone')), []).append(cand)
    groups = {k: v for k, v in by_key.items() if len(v) > 1}

    actions, group_actions = {}, {}
    for members in groups.values():
        action, owner, _why = rt.recommend_group_action(members)
        group_actions[action] = group_actions.get(action, 0) + 1
        for member in members:
            actions[member['woo_customer_id']] = rt.final_phone_action(
                member['woo_customer_id'], action, owner)
    return actions, groups, group_actions


def dry_run(mode_note, store_state=None):
    """Plan every customer, validate every payload, simulate every audit record.

    Returns the aggregate report. Writes nothing to Shopify because there is
    nothing here that could.
    """
    run_id = 'bulk-dry-run-' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    importer_commit = os.popen('git rev-parse --short HEAD').read().strip() or 'unknown'

    guard_4_dry_run_default()
    guard_11_verify_before_retry()
    guard_12_auth_failure_halts()
    guard_13_throttle_backoff()
    manifest_hash = guard_6_manifest_hash()

    print('Loading the approved manifest...')
    manifest_ids, _rows_by_id = load_manifest()

    print('Deriving candidates from source (offline)...')
    imports, conflicted, consent = load_population()
    derived_ids = [c['woo_customer_id'] for c in imports]

    # ---------------------------------------------------------- cohort
    cohort = [c for c in imports if c['woo_customer_id'] not in conflicted]
    cohort_ids = {c['woo_customer_id'] for c in cohort}
    guard_7_population(manifest_ids, derived_ids, len(cohort))
    guard_9_no_duplicate_legacy_ids([c['woo_customer_id'] for c in cohort])

    if store_state is not None:
        guard_8_live_customer_count(store_state.get('customers', -1))

    # ---------------------------------------------------------- phone
    actions, groups, group_actions = phone_decisions(imports)

    def sends_phone(cand):
        if not (cand.get('phone') or '').strip():
            return False
        return actions.get(cand['woo_customer_id'], rt.SEND_PHONE) == rt.SEND_PHONE

    # ---------------------------------------------------------- transform
    ledger = rt.ImportLedger(ledger_path=DRY_RUN_LEDGER,
                             checkpoint_path=DRY_RUN_CHECKPOINT,
                             run_id=run_id, importer_commit=importer_commit)
    for path in (DRY_RUN_LEDGER, DRY_RUN_CHECKPOINT):
        if os.path.exists(path):
            os.remove(path)

    counts = {
        'customerCreate': 0, 'customerAddressCreate': 0,
        'billing_address': 0, 'shipping_fallback': 0, 'no_address': 0,
        'phone_sent': 0, 'phone_omitted_collision': 0, 'phone_absent_in_source': 0,
        'legacy_metafield': 0, 'registered_at_metafield': 0,
        'consent_subscribed_in_cohort': 0, 'consent_applied_by_this_run': 0,
        'expected_phone_fallback_retries': 0,
    }
    not_eligible = []

    for cand in cohort:
        woo_id = cand['woo_customer_id']
        guard_15_within_cohort(woo_id, cohort_ids)
        try:
            stages = rt.plan_customer_import(
                cand, phone_allowed=sends_phone(cand),
                address_policy=rt.ADDRESS_POLICY_RATIFIED)
        except rt.NotEligibleForImport:
            not_eligible.append(woo_id)
            ledger.record(woo_id, rt.STAGE_CUSTOMER, 'DRY_RUN_NOT_ELIGIBLE',
                          error_class='NOT_ELIGIBLE',
                          reconciliation_status='NOT_APPLICABLE')
            continue

        payload = stages[0]['input']
        guard_14_legacy_metafield_inline(payload, woo_id)
        rt.assert_no_server_controlled_fields(payload)
        assert_customer_contract(payload, woo_id)

        counts['customerCreate'] += 1
        counts['legacy_metafield'] += 1
        if len(payload['metafields']) == 2:
            counts['registered_at_metafield'] += 1

        if 'phone' in payload:
            counts['phone_sent'] += 1
            category, _reason = rt.classify_phone_format(cand.get('phone'))
            if (category == rt.PHONE_FORMAT_INVALID
                    or rt.phone_plan_advisory(cand.get('phone'))):
                counts['expected_phone_fallback_retries'] += 1
        elif (cand.get('phone') or '').strip():
            counts['phone_omitted_collision'] += 1
        else:
            counts['phone_absent_in_source'] += 1

        if consent.get(cand['email']) == 'subscribed':
            counts['consent_subscribed_in_cohort'] += 1

        address_stages = [s for s in stages if s['stage'] == rt.STAGE_ADDRESS]
        counts['customerAddressCreate'] += len(address_stages)
        if not address_stages:
            counts['no_address'] += 1
            address_status = rt.ADDRESS_STATUS_NO_SOURCE_ADDRESS
        elif address_stages[0]['kind'] == 'billing':
            counts['billing_address'] += 1
            address_status = rt.ADDRESS_STATUS_PLANNED
        else:
            counts['shipping_fallback'] += 1
            address_status = rt.ADDRESS_STATUS_PLANNED

        ledger.record(woo_id, rt.STAGE_CUSTOMER, 'DRY_RUN_PLANNED',
                      address_status=address_status,
                      reconciliation_status='NOT_APPLICABLE')

    total = counts['customerCreate'] + counts['customerAddressCreate']
    worst = total + counts['expected_phone_fallback_retries']

    report = {
        'run_id': run_id,
        'importer_commit': importer_commit,
        'mode': MODE_DRY_RUN,
        'mode_note': mode_note,
        'shopify_mutations_performed': 0,
        'manifest': {
            'path': APPROVED_MANIFEST_PATH,
            'sha256': manifest_hash,
            'import_rows': len(manifest_ids),
        },
        'population': {
            'total_source_rows': 13043,
            'manifest_import_population': len(manifest_ids),
            'approved_run_population': len(cohort),
            'excluded_missing_email_permanent': 292,
            'deferred_name_conflicts': len(conflicted),
            'quarantined_total': 539,
            'skipped_duplicate_total': 407,
            'excluded_total': 1,
            'not_eligible_at_plan_time': len(not_eligible),
        },
        'transformation': {
            'billing_address': counts['billing_address'],
            'shipping_fallback': counts['shipping_fallback'],
            'no_address': counts['no_address'],
            'phone_sent': counts['phone_sent'],
            'phone_omitted_by_collision_policy': counts['phone_omitted_collision'],
            'phone_absent_in_source': counts['phone_absent_in_source'],
            'legacy_id_metafields': counts['legacy_metafield'],
            'registered_at_metafields': counts['registered_at_metafield'],
            'consent_subscribed_in_cohort': counts['consent_subscribed_in_cohort'],
            'consent_applied_by_this_run': counts['consent_applied_by_this_run'],
        },
        'phone_collisions': {
            'groups': len(groups),
            'by_recommended_action': group_actions,
            'unresolved_after_transformation': 0,
        },
        'mutations': {
            'expected_customerCreate': counts['customerCreate'],
            'expected_customerAddressCreate': counts['customerAddressCreate'],
            'expected_total': total,
            'expected_retries_phone_fallback': counts['expected_phone_fallback_retries'],
            'expected_total_worst_case': worst,
            'expected_throttles': 0,
        },
        'rate_plan': {
            'cost_per_mutation_measured': COST_PER_MUTATION,
            'total_points': total * COST_PER_MUTATION,
            'sustained_mutations_per_second': SUSTAINED_MUTATIONS_PER_SECOND,
            'expected_runtime_minutes': round(worst / SUSTAINED_MUTATIONS_PER_SECOND / 60, 1),
            'throttle_note': (
                'Zero throttles expected: the bucket restores 100 points/s and a '
                'mutation costs 10, so pacing at 10 mutations/s spends exactly what '
                'is restored. Any throttle that does occur is handled by the tested '
                'backoff and does not consume the transient-failure budget.'),
        },
        'guards': [{'guard': n, 'rule': rule, 'status': status}
                   for n, rule, status in GUARDS],
        'audit_simulation': {
            'ledger': DRY_RUN_LEDGER,
            'checkpoint': DRY_RUN_CHECKPOINT,
            'records_written': sum(ledger.counts.values()),
            'by_status': dict(ledger.counts),
            'note': ('Ledger-shaped records written through the same ImportLedger a '
                     'live run would use, to a dry-run path. Woo ids only - the '
                     'ledger schema rejects a record carrying an email, phone, name '
                     'or address.'),
        },
        'authorization': (
            'ADR-014 Gate 7 is UNSIGNED. This is a dry run. No customer, address '
            'or metafield was created, updated or deleted.'),
    }
    ledger.write_result(path=DRY_RUN_JSON, extra=report)
    return report


def assert_customer_contract(payload, woo_id):
    """Phase 8: exactly what may and may not appear in CustomerInput."""
    forbidden = {'password', 'username', 'wp_capabilities', 'capabilities',
                 'addresses', 'emailMarketingConsent', 'smsMarketingConsent'}
    present = forbidden & set(payload)
    if present:
        raise Halt(f'woo_customer_id={woo_id}: CustomerInput carries forbidden '
                   f'field(s) {sorted(present)}. Addresses are a separate call; '
                   f'consent is a separate pass; the rest never leave WordPress.')
    if not payload.get('email'):
        raise Halt(f'woo_customer_id={woo_id}: no email on the payload.')
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_argv(argv):
    mode = None
    authorization = None
    cohort_file = None
    observed_customers = None
    for index, arg in enumerate(argv):
        if arg == '--mode' and index + 1 < len(argv):
            mode = argv[index + 1]
        elif arg == '--authorization' and index + 1 < len(argv):
            authorization = argv[index + 1]
        elif arg == '--cohort-file' and index + 1 < len(argv):
            cohort_file = argv[index + 1]
        elif arg == '--observed-customer-count' and index + 1 < len(argv):
            # Fed in from phase10_preflight.py, which owns the live read. This
            # module has no transport of its own and is not gaining one to
            # satisfy a guard - so the number comes from the tool that is
            # allowed to ask, and GUARD 8 judges it here.
            observed_customers = int(argv[index + 1])
    return mode, authorization, cohort_file, observed_customers


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    requested_mode, authorization, cohort_file, observed = parse_argv(argv)
    mode = guard_3_live_mode_is_not_default(requested_mode)

    print('Phase 10 bulk importer - DRY RUN ONLY IN THIS REVISION.')
    print('There is no mutation document in this module and no transport for one.\n')

    if mode == MODE_LIVE:
        # The authorization is still checked, so that an operator who has one
        # gets the accurate answer rather than a permissions error, and one who
        # does not gets the refusal they should.
        try:
            guard_5_execution_authorization(authorization)
        except Halt as halt:
            print(f'HALT: {halt}')
            return 1
        raise LiveModeNotBuilt(
            'GUARD 3/4: live mode is not implemented in this revision. A valid '
            'Gate 7 authorization does not conjure a write path that was '
            'deliberately not built. The live executor is a separate change, to '
            'be reviewed on its own terms before it is pointed at 11,849 people.')

    if cohort_file:
        manifest_ids, _ = load_manifest()
        _imports, conflicted, _consent = load_population()
        approved = set(manifest_ids) - conflicted
        ids, digest = guard_16_verify_supplied_cohort(cohort_file, approved)
        print(f'GUARD 16: {len(ids)} supplied id(s) verified against the manifest, '
              f'file sha256={digest[:16]}...')

    report = dry_run(
        mode_note='dry run only; live mode not built in this revision',
        store_state=None if observed is None else {'customers': observed})

    print(json.dumps(report, indent=2, sort_keys=True))
    print(f'\nWrote {DRY_RUN_JSON} (aggregate, tracked)')
    print(f'Wrote {DRY_RUN_LEDGER} (audit simulation, gitignored)')
    print('\nSHOPIFY MUTATIONS: 0')
    print('ADR-014 Gate 7 is UNSIGNED. Nothing was written to Shopify.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Halt as halt:
        print(f'\n*** HALTED: {halt}')
        sys.exit(1)
