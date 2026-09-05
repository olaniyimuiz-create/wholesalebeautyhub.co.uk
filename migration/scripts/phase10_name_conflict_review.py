"""Phase 10 conflicting-identity reviewer file. OFFLINE ONLY - reads a file,
writes a file, contacts nothing.

247 quarantined rows share an email with an already-included customer but carry
a different name. The dry run refuses to merge them, which is correct and also
unfinished: somebody has to say which name is right.

NOTHING HERE GUESSES. WooCommerce records no ordering, no authority, and no
last-updated marker between the two variants, so there is no evidence to score
and no defensible automatic pick. Every chosen_name cell is written empty and
stays empty until a human fills it. A cell containing anything other than the
three permitted tokens raises rather than being interpreted.

The reviewer writes one of:

    IMPORT_NAME       the name the Shopify customer will be created with
    ALTERNATE_NAME    the name on the quarantined duplicate row
    MANUAL_REVIEW     cannot decide from the data; escalate

Tokens, not free text, so a mistyped name cannot silently become the customer's
name. resolve_chosen_name() in phase10_import_runtime maps the token back to the
actual string.

Reads  reports/phase10_customer_quarantine.csv   (produced by the dry run)
Writes reports/phase10_name_conflict_review.csv  GITIGNORED - real names/emails
       reports/phase10_name_conflict_summary.json TRACKED - counts only

Run: python migration/scripts/phase10_name_conflict_review.py
"""
import collections
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase10_import_runtime as rt

QUARANTINE_PATH = os.path.join('reports', 'phase10_customer_quarantine.csv')
REVIEW_PATH = os.path.join('reports', 'phase10_name_conflict_review.csv')
SUMMARY_PATH = os.path.join('reports', 'phase10_name_conflict_summary.json')

CONFLICT_REASON = 'duplicate_email_conflicting_identity'

# "Email already used by woo_customer_id=123 (Ada Lovelace) with a different
#  name here (Grace Hopper) - do not silently merge"
NOTE_RE = re.compile(
    r'woo_customer_id=(?P<import_id>\d+)\s*\((?P<import_name>.*?)\)\s*'
    r'with a different name here\s*\((?P<alternate_name>.*?)\)')


def _normalise(name):
    return re.sub(r'\s+', ' ', (name or '').strip()).lower()


def classify_conflict(import_name, alternate_name):
    """Triage only - how hard this row is to decide, NOT what to decide.

    Deliberately never touches chosen_name. The point is to let a reviewer sort
    153 near-mechanical rows from 94 that need real thought, not to make the
    call for them: even ALTERNATE_ONLY, where the imported customer would
    otherwise be created with no name at all, is a decision a human makes.
    """
    imp, alt = (import_name or '').strip(), (alternate_name or '').strip()
    if not imp and not alt:
        return 'BOTH_BLANK'
    if not imp:
        return 'IMPORT_BLANK_ALTERNATE_HAS_NAME'
    if not alt:
        return 'ALTERNATE_BLANK_IMPORT_HAS_NAME'
    if imp == alt:
        return 'IDENTICAL_AS_DISPLAYED_FIELD_SPLIT_DIFFERS'
    if _normalise(imp) == _normalise(alt):
        return 'DIFFERS_ONLY_BY_CASE_OR_WHITESPACE'
    return 'GENUINELY_DIFFERENT_NAMES'

# The seven columns requested, in order, followed by two traceability columns.
# woo_customer_id is the IMPORTED customer - the record that will exist in
# Shopify and whose name is in question. The quarantined duplicate's id is kept
# at the end so a reviewer can trace back to the source row.
REVIEW_FIELDS = [
    'woo_customer_id',
    'email',
    'import_name',
    'alternate_name',
    'chosen_name',
    'review_status',
    'reviewer',
    'alternate_row_woo_customer_id',
    'permitted_values',
    'conflict_class',
]


def build_rows(quarantine_path=QUARANTINE_PATH):
    rows, unparsed = [], []
    with open(quarantine_path, newline='', encoding='utf-8') as f:
        for record in csv.DictReader(f):
            if record.get('reason') != CONFLICT_REASON:
                continue
            match = NOTE_RE.search(record.get('notes') or '')
            if not match:
                # Never silently dropped: an unparsed conflict is a conflict
                # nobody reviews, which is worse than a loud failure.
                unparsed.append(record.get('woo_customer_id'))
                continue
            rows.append({
                'woo_customer_id': match.group('import_id'),
                'email': record.get('email', ''),
                'import_name': match.group('import_name').strip(),
                'alternate_name': match.group('alternate_name').strip(),
                'chosen_name': '',
                'review_status': rt.NAME_REVIEW_PENDING,
                'reviewer': '',
                'alternate_row_woo_customer_id': record.get('woo_customer_id', ''),
                'permitted_values': '|'.join(rt.VALID_NAME_CHOICES),
                'conflict_class': classify_conflict(match.group('import_name'),
                                                    match.group('alternate_name')),
            })
    return rows, unparsed


def main():
    if not os.path.exists(QUARANTINE_PATH):
        print(f'{QUARANTINE_PATH} not found - run phase10_customer_dry_run.py first')
        return 2

    rows, unparsed = build_rows()
    os.makedirs('reports', exist_ok=True)
    with open(REVIEW_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    unresolved = rt.unresolved_name_conflicts(rows)
    by_class = collections.Counter(r['conflict_class'] for r in rows)

    summary = {
        'conflicts': len(rows),
        'unparsed_quarantine_rows': len(unparsed),
        'resolved': len(rows) - len(unresolved),
        'unresolved': len(unresolved),
        'affected_import_customers': len({r['woo_customer_id'] for r in rows}),
        'conflicts_by_class': dict(sorted(by_class.items())),
        'genuinely_ambiguous': by_class.get('GENUINELY_DIFFERENT_NAMES', 0),
        'near_mechanical': len(rows) - by_class.get('GENUINELY_DIFFERENT_NAMES', 0),
        'permitted_chosen_name_values': list(rt.VALID_NAME_CHOICES),
        'default_policy': rt.POLICY_EXCLUDE_AFFECTED,
        'note': (
            'Nothing is pre-selected. Every chosen_name is empty and every review_status '
            'is PENDING. No name was guessed, merged, or overwritten. The affected '
            'customers are created either way - the open question is whether they are '
            'created carrying a name no human has confirmed.'),
    }

    blob = json.dumps(summary)
    for r in rows:
        for value in (r['email'], r['import_name'], r['alternate_name']):
            if value and str(value) in blob:
                raise SystemExit('ABORTED: PII reached the tracked summary')
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(json.dumps(summary, indent=2))
    print(f'\nWrote {REVIEW_PATH} ({len(rows)} rows) - CONTAINS PII, gitignored')
    print(f'Wrote {SUMMARY_PATH} - aggregate only, tracked')
    print('\nTriage (classification only - nothing pre-selected):')
    for name, count in sorted(by_class.items(), key=lambda kv: -kv[1]):
        print(f'  {count:>4}  {name}')
    if unparsed:
        print(f'\nWARNING: {len(unparsed)} conflict row(s) had an unrecognised note '
              f'format and were NOT written. Investigate - do not ignore.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
