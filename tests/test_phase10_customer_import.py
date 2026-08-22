"""Risk #45 - a rejected phone must cost the phone, never the customer.

Fully offline. Every `send` is a local callable returning a canned dict, no
credential is read, no socket is opened. Every email is example.com (RFC 2606)
and every phone is in Ofcom's 07700 900xxx drama range or an obvious invention -
no real customer data appears in this file.

Nothing here writes to a tracked report: the dropped-phone log is redirected to
a temporary directory for every test that produces one, so a test run can never
append to the real audit trail.

The bulk of the Phase 10 suite lives in migration/scripts/test_phase10_import_runtime.py
(a self-contained 640-assertion harness that predates this file). Rather than
duplicate it, test_no_regression_in_the_runtime_suite runs it and fails if a
single assertion in it fails, so `python -m unittest discover -s tests` really
is the whole gate.

Run: python -m unittest discover -s tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'migration', 'scripts')
sys.path.insert(0, SCRIPTS)

import phase10_import_runtime as rt          # noqa: E402
import phase10_test_import as executor       # noqa: E402


GID = 'gid://shopify/Customer/9999999999'
PHONE_INVALID_ERROR = [{'field': ['phone'], 'message': 'Phone is invalid'}]


def payload(**over):
    """A CustomerInput shaped exactly as build_customer_input produces one."""
    base = {
        'email': 'someone@example.com',
        'tags': ['imported-from-woocommerce', 'registered'],
        'metafields': [rt.legacy_metafield(4242)],
        'firstName': 'Ada',
        'lastName': 'Lovelace',
        'phone': '+447700900123',
    }
    base.update(over)
    return base


def created_response(sent_input, tags=None):
    """What Shopify returns for a successful customerCreate."""
    return {'data': {'customerCreate': {
        'customer': {
            'id': GID,
            'email': sent_input.get('email'),
            'firstName': sent_input.get('firstName'),
            'lastName': sent_input.get('lastName'),
            'phone': sent_input.get('phone'),
            'tags': tags if tags is not None else list(sent_input.get('tags') or []),
            'metafield': {'value': next(
                m['value'] for m in sent_input['metafields']
                if m['key'] == rt.LEGACY_KEY)},
        },
        'userErrors': [],
    }}}


def error_response(user_errors):
    """What Shopify returns when it rejects the whole mutation."""
    return {'data': {'customerCreate': {'customer': None, 'userErrors': user_errors}}}


class FakeShopify:
    """Canned responses in order. Records everything it was asked to send."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.sent = []

    def __call__(self, document, variables):
        self.sent.append({'document': document, 'variables': variables})
        if not self.queued:
            raise AssertionError('the executor sent more mutations than the test '
                                 'allowed for - a retry is looping')
        response = self.queued.pop(0)
        return response(variables['input']) if callable(response) else response

    @property
    def inputs(self):
        return [s['variables']['input'] for s in self.sent]


class TempLog(unittest.TestCase):
    """Redirects the dropped-phone log into a tempdir for the whole test."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self._dir.name, 'dropped_phones.jsonl')
        self.addCleanup(self._dir.cleanup)

    def events(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Detecting a phone error
# ---------------------------------------------------------------------------

class PhoneErrorDetection(unittest.TestCase):

    def test_field_list_names_phone(self):
        self.assertTrue(rt.is_phone_user_error(PHONE_INVALID_ERROR))

    def test_nested_input_path_still_names_phone(self):
        self.assertTrue(rt.is_phone_user_error(
            [{'field': ['input', 'phone'], 'message': 'Phone is invalid'}]))

    def test_bare_string_field(self):
        self.assertTrue(rt.is_phone_user_error(
            [{'field': 'phone', 'message': 'Phone is invalid'}]))

    def test_message_only_when_there_is_no_field(self):
        self.assertTrue(rt.is_phone_user_error(
            [{'field': None, 'message': 'Phone is invalid'}]))

    def test_other_fields_do_not_trigger_it(self):
        """An email error must not cause the phone to be dropped - the fallback
        would 'succeed' while quietly discarding data for no reason."""
        self.assertFalse(rt.is_phone_user_error(
            [{'field': ['email'], 'message': 'Email is invalid'}]))
        self.assertFalse(rt.is_phone_user_error([]))
        self.assertFalse(rt.is_phone_user_error(None))

    def test_a_message_that_merely_contains_a_number_does_not_match(self):
        """The message fallback is anchored, so an unrelated error that happens
        to quote a phone number cannot be read as a phone error."""
        self.assertFalse(rt.is_phone_user_error(
            [{'field': None, 'message': 'Address is invalid: call 07700900123'}]))

    def test_invalid_and_taken_are_distinguished(self):
        """Different upstream meanings: one is bad source data, the other is a
        collision the phone-collision review should have caught."""
        self.assertEqual(rt.classify_phone_error(PHONE_INVALID_ERROR),
                         rt.PHONE_DROP_INVALID)
        self.assertEqual(
            rt.classify_phone_error(
                [{'field': ['phone'], 'message': 'Phone has already been taken'}]),
            rt.PHONE_DROP_TAKEN)


# ---------------------------------------------------------------------------
# Building the retry payload
# ---------------------------------------------------------------------------

class StripPhoneForRetry(unittest.TestCase):

    def test_phone_is_removed_and_the_drop_is_tagged(self):
        retry = rt.strip_phone_for_retry(payload())
        self.assertNotIn('phone', retry)
        self.assertIn(rt.TAG_PHONE_DROPPED, retry['tags'])

    def test_legacy_metafield_survives_the_retry(self):
        """Idempotency depends on this: a customer created without the legacy id
        cannot be matched on resume and would be created a second time."""
        retry = rt.strip_phone_for_retry(payload())
        self.assertTrue(rt.assert_legacy_metafield_present(retry))
        self.assertEqual([m['value'] for m in retry['metafields']
                          if m['key'] == rt.LEGACY_KEY], ['4242'])

    def test_everything_else_is_untouched(self):
        original = payload()
        retry = rt.strip_phone_for_retry(original)
        for field in ('email', 'firstName', 'lastName'):
            self.assertEqual(retry[field], original[field])

    def test_the_original_payload_is_not_mutated(self):
        """The ledger and the reconciliation template describe the first
        payload; editing it in place would rewrite history to match the retry."""
        original = payload()
        rt.strip_phone_for_retry(original)
        self.assertEqual(original['phone'], '+447700900123')
        self.assertNotIn(rt.TAG_PHONE_DROPPED, original['tags'])

    def test_metafields_are_copied_not_shared(self):
        original = payload()
        retry = rt.strip_phone_for_retry(original)
        retry['metafields'][0]['value'] = 'tampered'
        self.assertEqual(original['metafields'][0]['value'], '4242')

    def test_tag_is_not_duplicated(self):
        already = payload(tags=['imported-from-woocommerce', rt.TAG_PHONE_DROPPED])
        retry = rt.strip_phone_for_retry(already)
        self.assertEqual(retry['tags'].count(rt.TAG_PHONE_DROPPED), 1)

    def test_a_payload_with_no_phone_is_refused(self):
        """Nothing to drop means the retry would send the identical payload and
        get the identical error. That is a loop, so it raises."""
        no_phone = {k: v for k, v in payload().items() if k != 'phone'}
        with self.assertRaises(rt.PhoneFallbackNotApplicable):
            rt.strip_phone_for_retry(no_phone)


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------

class DroppedPhoneLog(TempLog):

    def test_the_event_records_what_was_lost(self):
        result = rt.phone_fallback(payload(), PHONE_INVALID_ERROR, 4242,
                                   run_id='test-run', log_path=self.log_path)
        event = result['event']
        self.assertEqual(event['woo_customer_id'], 4242)
        self.assertEqual(event['phone_original'], '+447700900123')
        self.assertEqual(event['reason'], rt.PHONE_DROP_INVALID)
        self.assertEqual(event['operation'], 'customerCreate')
        self.assertEqual(event['source'], rt.DROP_SOURCE_RETRY)
        self.assertTrue(event['customer_preserved'])

    def test_it_is_written_to_the_log_file(self):
        rt.phone_fallback(payload(), PHONE_INVALID_ERROR, 4242,
                          run_id='test-run', log_path=self.log_path)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['woo_customer_id'], 4242)

    def test_the_log_carries_a_phone_but_nothing_else(self):
        """This file is the one deliberate exception to the no-PII rule. It must
        not quietly grow an email or an address alongside it."""
        rt.phone_fallback(payload(), PHONE_INVALID_ERROR, 4242, log_path=self.log_path)
        written = open(self.log_path, encoding='utf-8').read()
        self.assertNotIn('someone@example.com', written)
        self.assertNotIn('Lovelace', written)
        with self.assertRaises(ValueError):
            rt.assert_dropped_phone_record_safe(
                {'woo_customer_id': 1, 'email': 'someone@example.com'})

    def test_shopify_error_text_is_sanitized(self):
        """Shopify echoes the submitted value back in userErrors."""
        rt.phone_fallback(
            payload(),
            [{'field': ['phone'], 'message': 'Phone +447700900123 is invalid'}],
            4242, log_path=self.log_path)
        event = self.events()[0]
        self.assertIn('[PHONE_REDACTED]', event['user_errors'][0]['message'])

    def test_a_non_phone_error_is_refused(self):
        with self.assertRaises(rt.PhoneFallbackNotApplicable):
            rt.phone_fallback(payload(),
                              [{'field': ['email'], 'message': 'Email is invalid'}],
                              4242, log_path=self.log_path)
        self.assertEqual(self.events(), [])

    def test_offline_flags_share_the_log_and_are_distinguishable(self):
        rt.append_dropped_phone(
            rt.offline_phone_flag_event(7, '+4477009001',
                                        rt.ADVISORY_GB_NSN_LENGTH, run_id='pre'),
            path=self.log_path)
        rt.phone_fallback(payload(), PHONE_INVALID_ERROR, 4242,
                          log_path=self.log_path)
        sources = [e['source'] for e in self.events()]
        self.assertEqual(sources, [rt.DROP_SOURCE_PRECHECK, rt.DROP_SOURCE_RETRY])


# ---------------------------------------------------------------------------
# The behaviour that actually matters: the customer survives
# ---------------------------------------------------------------------------

class RetryWithoutPhone(TempLog):

    def create(self, fake, sent=None, woo_id=4242):
        return executor.create_customer(fake, sent if sent is not None else payload(),
                                        woo_id, 'test-run', log_path=self.log_path)

    def test_a_phone_error_no_longer_loses_the_customer(self):
        """The regression this whole change exists for. Before it, this exact
        response produced zero customers."""
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        result, errors, attempts, event = self.create(fake)

        self.assertEqual(errors, [])
        self.assertEqual(result['customer']['id'], GID)
        self.assertEqual(attempts, 2)
        self.assertIsNotNone(event)

    def test_the_retry_carries_no_phone(self):
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        self.create(fake)
        first, second = fake.inputs
        self.assertIn('phone', first)
        self.assertNotIn('phone', second)

    def test_the_retry_is_tagged_and_keeps_its_identity(self):
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        self.create(fake)
        second = fake.inputs[1]
        self.assertIn(rt.TAG_PHONE_DROPPED, second['tags'])
        self.assertEqual([m['value'] for m in second['metafields']
                          if m['key'] == rt.LEGACY_KEY], ['4242'])
        self.assertEqual(second['email'], 'someone@example.com')

    def test_exactly_two_mutations_are_sent(self):
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        self.create(fake)
        self.assertEqual(len(fake.sent), 2)

    def test_the_drop_is_logged(self):
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        self.create(fake)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['phone_original'], '+447700900123')
        self.assertEqual(events[0]['run_id'], 'test-run')

    def test_a_clean_create_neither_retries_nor_logs(self):
        fake = FakeShopify(created_response)
        result, errors, attempts, event = self.create(fake)
        self.assertEqual(attempts, 1)
        self.assertIsNone(event)
        self.assertEqual(errors, [])
        self.assertEqual(len(fake.sent), 1)
        self.assertIn('phone', fake.inputs[0])
        self.assertEqual(self.events(), [])

    def test_the_retry_happens_once_and_only_once(self):
        """A second phone error on a payload that no longer has a phone is not a
        phone problem. FakeShopify raises if a third mutation is attempted."""
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR),
                           error_response(PHONE_INVALID_ERROR))
        result, errors, attempts, event = self.create(fake)
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(attempts, 2)
        self.assertTrue(errors)
        self.assertIsNone(result['customer'])

    def test_a_non_phone_failure_is_not_retried(self):
        """An invalid email is not fixed by dropping a phone, and retrying would
        double the cost of every genuinely broken record."""
        fake = FakeShopify(error_response(
            [{'field': ['email'], 'message': 'Email is invalid'}]))
        result, errors, attempts, event = self.create(fake)
        self.assertEqual(attempts, 1)
        self.assertIsNone(event)
        self.assertTrue(errors)
        self.assertEqual(self.events(), [])

    def test_a_phone_error_on_a_payload_with_no_phone_is_not_retried(self):
        """Belt and braces: the collision review already omits some phones."""
        no_phone = {k: v for k, v in payload().items() if k != 'phone'}
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR))
        _result, _errors, attempts, event = self.create(fake, sent=no_phone)
        self.assertEqual(attempts, 1)
        self.assertIsNone(event)

    def test_the_retry_payload_faces_the_same_guards(self):
        """The retry is a fresh payload, not a trusted one. A payload carrying
        consent must still be refused on the second attempt."""
        unsafe = payload(emailMarketingConsent={'marketingState': 'SUBSCRIBED'})
        fake = FakeShopify(error_response(PHONE_INVALID_ERROR), created_response)
        with self.assertRaises(executor.Halt):
            self.create(fake, sent=unsafe)

    def test_a_taken_number_is_dropped_and_recorded_as_such(self):
        fake = FakeShopify(
            error_response([{'field': ['phone'],
                             'message': 'Phone has already been taken'}]),
            created_response)
        _result, errors, _attempts, event = self.create(fake)
        self.assertEqual(errors, [])
        self.assertEqual(event['reason'], rt.PHONE_DROP_TAKEN)


# ---------------------------------------------------------------------------
# Offline format pre-check
# ---------------------------------------------------------------------------

class PhoneFormatClassification(unittest.TestCase):

    def test_plus_prefixed_international_is_valid(self):
        self.assertEqual(rt.classify_phone_format('+447700900123')[0],
                         rt.PHONE_FORMAT_VALID)

    def test_uk_national_format_needs_normalization(self):
        """Not a failure: Shopify normalises 07... to +44... at write time, as
        the Gate 6 cohort confirmed."""
        for value in ('07700900123', '07700 900123', '(07700) 900123'):
            self.assertEqual(rt.classify_phone_format(value)[0],
                             rt.PHONE_FORMAT_NEEDS_NORMALIZATION, value)

    def test_00_prefix_needs_normalization(self):
        category, reason = rt.classify_phone_format('00447700900123')
        self.assertEqual(category, rt.PHONE_FORMAT_NEEDS_NORMALIZATION)
        self.assertEqual(reason, rt.REASON_INTL_PREFIX_00)

    def test_too_few_and_too_many_digits_are_invalid(self):
        self.assertEqual(rt.classify_phone_format('077009')[:2],
                         (rt.PHONE_FORMAT_INVALID, rt.REASON_TOO_FEW))
        self.assertEqual(rt.classify_phone_format('+4477009001234567')[:2],
                         (rt.PHONE_FORMAT_INVALID, rt.REASON_TOO_MANY))

    def test_letters_and_stray_text_are_invalid(self):
        for value in ('not a phone', '07700900123 ext 4', '07700900123/ask for Ada'):
            self.assertEqual(rt.classify_phone_format(value)[0],
                             rt.PHONE_FORMAT_INVALID, value)

    def test_a_misplaced_plus_is_invalid(self):
        self.assertEqual(rt.classify_phone_format('++447700900123')[1],
                         rt.REASON_PLUS_MISPLACED)
        self.assertEqual(rt.classify_phone_format('0770+0900123')[1],
                         rt.REASON_PLUS_MISPLACED)

    def test_no_phone_is_its_own_category(self):
        for value in (None, '', '   '):
            self.assertEqual(rt.classify_phone_format(value)[0], rt.PHONE_FORMAT_ABSENT)

    def test_the_shape_that_lost_woo_1_is_caught_by_the_advisory(self):
        """+44 7... with a nine-digit national number. Structurally impeccable,
        rejected live. If a generic length check were enough, risk #45 would
        have been an offline finding rather than a production one."""
        truncated = '+44770090012'
        self.assertEqual(rt.classify_phone_format(truncated)[0], rt.PHONE_FORMAT_VALID)
        self.assertEqual(rt.phone_plan_advisory(truncated), rt.ADVISORY_GB_NSN_LENGTH)

    def test_a_correct_gb_mobile_raises_no_advisory(self):
        self.assertIsNone(rt.phone_plan_advisory('+447700900123'))
        self.assertIsNone(rt.phone_plan_advisory('07700900123'))

    def test_non_gb_numbers_are_left_alone_by_the_advisory(self):
        """The plan table covers GB only, and guessing at other countries would
        manufacture false alarms in a report meant to size a real problem."""
        self.assertIsNone(rt.phone_plan_advisory('+35312345678'))
        self.assertIsNone(rt.phone_plan_advisory('+12025550143'))

    def test_classification_never_rewrites_the_value(self):
        original = '07700 900123'
        rt.classify_phone_format(original)
        self.assertEqual(original, '07700 900123')

    def test_summary_is_aggregate_only(self):
        candidates = [
            {'woo_customer_id': 1, 'phone': '+447700900123'},
            {'woo_customer_id': 2, 'phone': '07700900123'},
            {'woo_customer_id': 3, 'phone': '123'},
            {'woo_customer_id': 4, 'phone': ''},
        ]
        summary = rt.phone_format_summary(candidates)
        self.assertEqual(summary['customers_scanned'], 4)
        self.assertEqual(summary['categories'][rt.PHONE_FORMAT_VALID], 1)
        self.assertEqual(summary['categories'][rt.PHONE_FORMAT_NEEDS_NORMALIZATION], 1)
        self.assertEqual(summary['categories'][rt.PHONE_FORMAT_INVALID], 1)
        self.assertEqual(summary['categories'][rt.PHONE_FORMAT_ABSENT], 1)
        blob = json.dumps(summary)
        for candidate in candidates:
            if candidate['phone']:
                self.assertNotIn(candidate['phone'], blob)


class PhoneValidationSummaryReport(unittest.TestCase):
    """The generated report itself, if it has been produced."""

    PATH = os.path.join(REPO_ROOT, 'reports', 'phase10_phone_validation_summary.json')

    def setUp(self):
        if not os.path.exists(self.PATH):
            self.skipTest('run migration/scripts/phase10_phone_format_validator.py first')
        with open(self.PATH, encoding='utf-8') as f:
            self.summary = json.load(f)

    def test_it_covers_the_whole_import_population(self):
        self.assertEqual(self.summary['customers_scanned'], 12096)
        self.assertEqual(self.summary['customers_with_phone'], 4450)

    def test_it_made_no_shopify_request(self):
        self.assertEqual(self.summary['shopify_requests'], 0)

    def test_it_states_what_it_cannot_prove(self):
        """A structural check reported as a guarantee is how risk #45 stayed
        invisible in the first place."""
        self.assertIn('STRUCTURAL ONLY', self.summary['validation_limits'])

    def test_the_tracked_summary_carries_no_phone_number(self):
        """Counts are fine; a nine-digit-or-longer run of digits is not, because
        that is the shape of a phone number and nothing else in this file."""
        runs = re.findall(r'\d{9,}', json.dumps(self.summary))
        self.assertEqual(runs, [])


# ---------------------------------------------------------------------------
# Gate 2 - address policy A_PLUS (decided 2026-08-22)
# ---------------------------------------------------------------------------

def addr_candidate(**over):
    """A candidate with both addresses. All values invented."""
    base = {
        'woo_customer_id': 4242, 'email': 'someone@example.com',
        'email_raw': 'someone@example.com', 'first_name': 'Ada',
        'last_name': 'Lovelace', 'company': '',
        'billing_address1': '1 Test Street', 'billing_address2': '',
        'billing_city': 'Testville', 'billing_province': 'Surrey',
        'billing_country': 'GB', 'billing_zip': 'SW1A 1AA',
        'shipping_address1': '2 Other Road', 'shipping_address2': '',
        'shipping_city': 'Otherton', 'shipping_province': '',
        'shipping_country': 'GB', 'shipping_zip': 'M1 1AE',
        'is_registered': True, 'date_registered': '2021-03-04 10:00:00',
    }
    base.update(over)
    return base


def no_billing(**over):
    return addr_candidate(billing_address1='', billing_city='', billing_province='',
                          billing_country='', billing_zip='', **over)


class AddressPolicy(unittest.TestCase):

    def kinds(self, candidate, policy):
        return [p['kind'] for p in rt.plan_addresses(candidate, policy=policy)]

    def test_the_ratified_policy_is_a_plus(self):
        self.assertEqual(rt.ADDRESS_POLICY_RATIFIED,
                         rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING)

    def test_a_plus_sends_billing_when_there_is_one(self):
        self.assertEqual(self.kinds(addr_candidate(),
                                    rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING),
                         ['billing'])

    def test_a_plus_falls_back_to_shipping_when_there_is_no_billing(self):
        """The 17 customers option A would have left with no address at all,
        despite usable address data sitting in the source."""
        self.assertEqual(self.kinds(no_billing(),
                                    rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING),
                         ['shipping'])

    def test_the_fallback_address_becomes_their_default(self):
        plan = rt.plan_addresses(no_billing(),
                                 policy=rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING)
        self.assertTrue(plan[0]['setAsDefault'])

    def test_a_plus_never_sends_two(self):
        """The whole reason A_PLUS was chosen over B: at most one address, so no
        customer carries a second one that Shopify renders unlabelled."""
        for candidate in (addr_candidate(), no_billing()):
            plan = rt.plan_addresses(candidate,
                                     policy=rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING)
            self.assertLessEqual(len(plan), 1)

    def test_a_plus_falls_back_when_billing_is_skipped_not_merely_absent(self):
        """A billing address with no country is SKIPPED under the ratified
        country rule. That customer has no billing address in Shopify's terms,
        so the fallback must fire for them too."""
        candidate = addr_candidate(billing_country='')
        self.assertEqual(self.kinds(candidate,
                                    rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING),
                         ['shipping'])

    def test_a_customer_with_no_address_at_all_still_gets_none(self):
        candidate = no_billing(shipping_address1='')
        self.assertEqual(self.kinds(candidate,
                                    rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING), [])

    def test_option_a_and_option_b_still_behave_as_measured(self):
        """Both remain implemented: the decision is recorded in a document, and
        a document can be revised."""
        self.assertEqual(self.kinds(addr_candidate(),
                                    rt.ADDRESS_POLICY_BILLING_ONLY), ['billing'])
        self.assertEqual(self.kinds(no_billing(),
                                    rt.ADDRESS_POLICY_BILLING_ONLY), [])
        self.assertEqual(self.kinds(addr_candidate(),
                                    rt.ADDRESS_POLICY_BILLING_PLUS_SHIPPING),
                         ['billing', 'shipping'])

    def test_the_include_shipping_switch_still_means_what_it_did(self):
        """644 assertions and several scripts were written against it."""
        self.assertEqual([p['kind'] for p in
                          rt.plan_addresses(addr_candidate(), include_shipping=False)],
                         ['billing'])
        self.assertEqual([p['kind'] for p in
                          rt.plan_addresses(addr_candidate(), include_shipping=True)],
                         ['billing', 'shipping'])

    def test_an_unimplemented_policy_raises_rather_than_guessing(self):
        """Falling back to a real policy here would import thousands of
        customers under a rule nobody chose."""
        with self.assertRaises(rt.UnknownAddressPolicy):
            rt.plan_addresses(addr_candidate(), policy='A_PLUS_PLUS')

    def test_the_policy_reaches_the_stage_plan(self):
        stages = rt.plan_customer_import(
            no_billing(), address_policy=rt.ADDRESS_POLICY_BILLING_ELSE_SHIPPING)
        self.assertEqual(stages[0]['stage'], rt.STAGE_CUSTOMER)
        self.assertEqual([s['kind'] for s in stages[1:]], ['shipping'])

    def test_the_measured_report_matches_the_selected_policy(self):
        path = os.path.join(REPO_ROOT, 'reports', 'phase10_address_readiness.json')
        if not os.path.exists(path):
            self.skipTest('run phase10_address_readiness.py first')
        with open(path, encoding='utf-8') as f:
            report = json.load(f)
        self.assertEqual(report['gate_2_selected_policy'], rt.ADDRESS_POLICY_RATIFIED)
        self.assertEqual(
            report['address_calls_option_a_plus_billing_else_shipping'],
            report['address_calls_option_a_billing_only']
            + report['customers_with_shipping_but_no_billing'])


# ---------------------------------------------------------------------------
# Bulk importer - the sixteen guards (Phase 3)
# ---------------------------------------------------------------------------

import phase10_bulk_import as bulk       # noqa: E402


class BulkImporterGuards(unittest.TestCase):
    """Each guard must HALT, not warn. A guard that returns a warning is a
    guard that gets ignored at 2am."""

    def test_guard_1_rejects_any_store_but_the_approved_one(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_1_approved_store('some-other-store.myshopify.com')
        self.assertTrue(bulk.guard_1_approved_store(bulk.APPROVED_STORE_DOMAIN))

    def test_guard_2_rejects_a_production_store(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_2_reject_production(
                {'plan': {'partnerDevelopment': False, 'displayName': 'Shopify Plus'},
                 'myshopifyDomain': 'shop.myshopify.com'})

    def test_guard_2_treats_a_missing_development_flag_as_production(self):
        """The safe reading of missing information. A store that does not say
        it is a development store is not one."""
        with self.assertRaises(bulk.Halt):
            bulk.guard_2_reject_production({'plan': {}, 'myshopifyDomain': 'x'})

    def test_guard_2_rejects_a_production_marker_in_the_domain(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_2_reject_production(
                {'plan': {'partnerDevelopment': True},
                 'myshopifyDomain': 'beautyhub-production.myshopify.com'})

    def test_guard_3_defaults_to_dry_run_when_no_mode_is_given(self):
        self.assertEqual(bulk.guard_3_live_mode_is_not_default(None), bulk.MODE_DRY_RUN)

    def test_guard_3_rejects_an_unknown_mode(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_3_live_mode_is_not_default('yolo')

    def test_guard_4_dry_run_is_the_module_default(self):
        self.assertEqual(bulk.DEFAULT_MODE, bulk.MODE_DRY_RUN)
        self.assertTrue(bulk.guard_4_dry_run_default())

    def test_guard_5_requires_the_exact_authorization_phrase(self):
        for wrong in (None, '', 'approved', 'APPROVED - EXECUTE GATE 7',
                      'approved - execute gate 7 bulk customer import',
                      'Gate 6 was approved so Gate 7 is too'):
            with self.assertRaises(bulk.Halt):
                bulk.guard_5_execution_authorization(wrong)
        self.assertTrue(
            bulk.guard_5_execution_authorization(bulk.GATE_7_AUTHORIZATION))

    def test_guard_6_matches_the_approved_manifest_hash(self):
        path = os.path.join(REPO_ROOT, bulk.APPROVED_MANIFEST_PATH)
        if not os.path.exists(path):
            self.skipTest('manifest not present')
        self.assertEqual(bulk.guard_6_manifest_hash(path),
                         bulk.APPROVED_MANIFEST_SHA256)

    def test_guard_6_halts_on_a_changed_manifest(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False) as handle:
            handle.write('woo_customer_id,classification\n1,IMPORT\n')
            path = handle.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(bulk.Halt):
            bulk.guard_6_manifest_hash(path)

    def test_guard_7_halts_when_the_population_count_is_wrong(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_7_population([1, 2, 3], [1, 2, 3], 11849)

    def test_guard_7_halts_when_the_ids_differ_at_the_same_count(self):
        """Two different populations of the same size pass a count check and
        import the wrong people, so the sets are compared, not the totals."""
        manifest = list(range(bulk.APPROVED_IMPORT_POPULATION))
        derived = list(range(1, bulk.APPROVED_IMPORT_POPULATION + 1))
        with self.assertRaises(bulk.Halt):
            bulk.guard_7_population(manifest, derived, 11849)

    def test_guard_7_halts_on_the_wrong_run_population(self):
        ids = list(range(bulk.APPROVED_IMPORT_POPULATION))
        with self.assertRaises(bulk.Halt):
            bulk.guard_7_population(ids, ids, 12096)

    def test_guard_8_halts_unless_the_store_is_in_the_expected_state(self):
        self.assertTrue(bulk.guard_8_live_customer_count(0))
        for count in (1, 7, 11849):
            with self.assertRaises(bulk.Halt):
                bulk.guard_8_live_customer_count(count)

    def test_guard_9_halts_on_a_duplicate_legacy_id(self):
        self.assertTrue(bulk.guard_9_no_duplicate_legacy_ids([1, 2, 3]))
        with self.assertRaises(bulk.Halt):
            bulk.guard_9_no_duplicate_legacy_ids([1, 2, 2, 3])

    def test_guard_10_skips_a_customer_already_present(self):
        self.assertEqual(
            bulk.guard_10_skip_if_present(5, {'5': {'gid': 'gid://shopify/Customer/5'}}),
            'gid://shopify/Customer/5')
        self.assertIsNone(bulk.guard_10_skip_if_present(5, {}))

    def test_guards_11_12_13_are_delegated_and_still_present(self):
        """They are the runtime's, and the runtime is what a live executor will
        use. Asserted rather than assumed, so removing one fails here."""
        self.assertTrue(bulk.guard_11_verify_before_retry())
        self.assertTrue(bulk.guard_12_auth_failure_halts())
        self.assertTrue(bulk.guard_13_throttle_backoff())

    def test_guard_14_halts_without_the_legacy_metafield(self):
        with self.assertRaises(bulk.Halt):
            bulk.guard_14_legacy_metafield_inline({'email': 'a@example.com'}, 1)
        self.assertTrue(bulk.guard_14_legacy_metafield_inline(payload(), 4242))

    def test_guard_15_refuses_anything_outside_the_cohort(self):
        self.assertTrue(bulk.guard_15_within_cohort(1, {1, 2}))
        with self.assertRaises(bulk.Halt):
            bulk.guard_15_within_cohort(999, {1, 2})

    def test_guard_16_refuses_a_supplied_list_with_an_outsider(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as handle:
            handle.write('1\n2\n999\n')
            path = handle.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(bulk.Halt):
            bulk.guard_16_verify_supplied_cohort(path, {1, 2, 3})

    def test_guard_16_accepts_a_verified_list_and_records_its_hash(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as handle:
            handle.write('1\n2\n')
            path = handle.name
        self.addCleanup(os.unlink, path)
        ids, digest = bulk.guard_16_verify_supplied_cohort(path, {1, 2, 3})
        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(digest), 64)

    def test_all_sixteen_guards_are_registered(self):
        self.assertEqual([n for n, _, _ in bulk.GUARDS], list(range(1, 17)))


class BulkImporterCannotWrite(unittest.TestCase):
    """The property that makes this revision safe to review is that it has no
    write path at all - not that its write path is switched off."""

    SOURCE = os.path.join(SCRIPTS, 'phase10_bulk_import.py')

    def test_the_module_contains_no_graphql_mutation_document(self):
        source = open(self.SOURCE, encoding='utf-8').read()
        for name in ('customerCreate(', 'customerAddressCreate(', 'customerDelete('):
            self.assertNotIn(name, source)

    def test_it_imports_no_transport(self):
        source = open(self.SOURCE, encoding='utf-8').read()
        for forbidden in ('graphql_request', 'urllib.request', 'import requests'):
            self.assertNotIn(forbidden, source)

    def test_live_mode_is_refused_even_with_a_valid_authorization(self):
        """A valid Gate 7 phrase does not conjure a write path that was
        deliberately not built."""
        with self.assertRaises(bulk.LiveModeNotBuilt):
            bulk.main(['--mode', 'live',
                       '--authorization', bulk.GATE_7_AUTHORIZATION])

    def test_live_mode_without_authorization_halts_first(self):
        self.assertEqual(bulk.main(['--mode', 'live']), 1)

    def test_the_runtime_it_builds_on_still_refuses_mutations(self):
        with self.assertRaises(rt.HaltMigration):
            rt.execute_with_retry(lambda d, v: {}, 'mutation m { customerCreate }')


# ---------------------------------------------------------------------------
# Guarantees this change must not have weakened
# ---------------------------------------------------------------------------

class ExistingGuaranteesHold(unittest.TestCase):

    def test_the_runtime_still_refuses_to_send_a_mutation(self):
        """The fallback decides and transforms; it does not send. The runtime's
        structural inability to write is the reason it can be trusted."""
        with self.assertRaises(rt.HaltMigration):
            rt.execute_with_retry(lambda d, v: {}, 'mutation x { customerCreate }')

    def test_the_import_ledger_still_forbids_phone(self):
        self.assertIn('phone', rt.LEDGER_FORBIDDEN_KEYS)
        with self.assertRaises(ValueError):
            rt.assert_ledger_record_safe({'woo_customer_id': 1, 'phone': '07700900123'})

    def test_importing_the_executor_sends_nothing(self):
        """phase10_test_import is the only script that writes. Importing it for
        these tests must not be capable of starting a run."""
        self.assertNotIn('--execute', sys.argv)
        self.assertEqual(executor.MAX_COHORT, 10)

    def test_no_regression_in_the_runtime_suite(self):
        """Runs the full offline Phase 10 harness. If any assertion in it fails,
        this fails - so unittest discovery is the whole gate, not a slice."""
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, 'test_phase10_import_runtime.py')],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
        tail = '\n'.join(proc.stdout.strip().splitlines()[-25:])
        self.assertEqual(proc.returncode, 0, f'runtime suite failed:\n{tail}')
        self.assertIn('passed.', proc.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
