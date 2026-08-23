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
# Tier-3 live-test executor (Approval A - build only)
# ---------------------------------------------------------------------------
#
# Every test here runs against a mock. No credential is read, no socket is
# opened, and no Shopify mutation is sent. All customer data is invented:
# example.com emails, Ofcom 07700 900xxx phones, made-up streets.

import phase10_tier3_executor as tier3    # noqa: E402

# Underscores deliberately: this must NOT match the shpca_[A-Za-z0-9]+
# credential pattern. A synthetic value shaped exactly like a real token
# is a value that trips secret scanners and pages someone at 3am.
FAKE_TOKEN = 'shpca_NOT_A_REAL_TOKEN_synthetic_value_for_tests_only'
SYNTHETIC_PII = {
    'email': 'tier3-subject@example.com',
    'first_name': 'Grace',
    'last_name': 'Hopper',
    'phone': '+447700900456',
    'address1': '5 Invented Lane',
    'zip': 'EC1A 1BB',
}


def tier3_candidate(woo_id, registered=True, with_address=False, country='GB'):
    """A synthetic manifest row. Never a real customer."""
    return {
        'woo_customer_id': woo_id,
        'email': SYNTHETIC_PII['email'], 'email_raw': SYNTHETIC_PII['email'],
        'first_name': SYNTHETIC_PII['first_name'],
        'last_name': SYNTHETIC_PII['last_name'],
        'company': '', 'phone': SYNTHETIC_PII['phone'],
        'is_registered': registered,
        'date_registered': '2021-03-04 10:00:00' if registered else '',
        'billing_address1': SYNTHETIC_PII['address1'] if with_address else '',
        'billing_address2': '',
        'billing_city': 'Testville' if with_address else '',
        'billing_province': 'Surrey' if with_address else '',
        'billing_country': country if with_address else '',
        'billing_zip': SYNTHETIC_PII['zip'] if with_address else '',
        'shipping_address1': '', 'has_shipping_address': False,
    }


def loader_for(candidate):
    def _load(woo_id, *_a, **_k):
        record = dict(candidate)
        record['woo_customer_id'] = woo_id
        return record
    return _load


PREFLIGHT_OK = {
    'data': {
        'shop': {'name': 'Wholesale Beautyhub',
                 'myshopifyDomain': tier3.APPROVED_STORE_DOMAIN,
                 'plan': {'displayName': 'Grow', 'partnerDevelopment': True}},
        'currentAppInstallation': {'accessScopes': [
            {'handle': 'read_customers'}, {'handle': 'write_customers'}]},
        'customersCount': {'count': 0},
        '__type': {'inputFields': [{'name': n} for n in
                                   ('email', 'firstName', 'lastName', 'phone',
                                    'tags', 'metafields')]},
    }
}

NO_LEGACY_MATCH = {'data': {'customers': {'edges': []}}}


def created_customer(variables, woo_id):
    sent = variables['input']
    return {'data': {'customerCreate': {
        'customer': {
            'id': f'gid://shopify/Customer/{700000 + woo_id}',
            'email': sent.get('email'), 'firstName': sent.get('firstName'),
            'lastName': sent.get('lastName'), 'phone': sent.get('phone'),
            'tags': sent.get('tags'), 'createdAt': '2026-08-22T00:00:00Z',
            'metafields': {'edges': [{'node': {'key': m['key'], 'value': m['value'],
                                               'type': m['type']}}
                                     for m in sent['metafields']]},
        },
        'userErrors': [],
    }}}


def created_address(variables):
    sent = variables['address']
    return {'data': {'customerAddressCreate': {
        'address': dict(sent, id='gid://shopify/MailingAddress/1',
                        countryCodeV2=sent.get('countryCode')),
        'userErrors': [],
    }}}


class MockShopify:
    """Dispatches by document. Records everything. Never touches a network."""

    def __init__(self, preflight=None, legacy=None, woo_id=220,
                 create_behaviour=None, address_behaviour=None):
        self.preflight = preflight or PREFLIGHT_OK
        self.legacy = legacy or NO_LEGACY_MATCH
        self.woo_id = woo_id
        self.create_behaviour = create_behaviour
        self.address_behaviour = address_behaviour
        self.sent = []
        self.variables_sent = []
        self.mutations_sent = 0

    def __call__(self, document, variables=None):
        self.sent.append(document)
        self.variables_sent.append(variables)
        if document.strip().startswith('mutation'):
            self.mutations_sent += 1
        if 'shop {' in document:
            return self.preflight
        if 'customers(first: 5' in document:
            return self.legacy if not callable(self.legacy) else self.legacy()
        if 'customerCreate' in document:
            if callable(self.create_behaviour):
                return self.create_behaviour(variables, len(self.sent))
            return self.create_behaviour or created_customer(variables, self.woo_id)
        if 'customerAddressCreate' in document:
            if callable(self.address_behaviour):
                return self.address_behaviour(variables)
            return self.address_behaviour or created_address(variables)
        raise AssertionError(f'unexpected document: {document[:60]}')

    @property
    def documents_sent(self):
        return list(self.sent)

    @property
    def inputs(self):
        """The CustomerInput payloads actually sent, in order."""
        return [v['input'] for v in self.variables_sent
                if isinstance(v, dict) and 'input' in v]


class Tier3ExecutorBase(unittest.TestCase):
    """Redirects the audit destinations into a tempdir for every test."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._ledger, self._checkpoint = tier3.LEDGER_PATH, tier3.CHECKPOINT_PATH
        tier3.LEDGER_PATH = os.path.join(self._dir.name, 'tier3.jsonl')
        tier3.CHECKPOINT_PATH = os.path.join(self._dir.name, 'tier3_checkpoint.jsonl')
        self.addCleanup(self._restore)
        self.commit = tier3.reviewed_commit()

    def _restore(self):
        tier3.LEDGER_PATH, tier3.CHECKPOINT_PATH = self._ledger, self._checkpoint

    def ledger_text(self):
        out = ''
        for path in (tier3.LEDGER_PATH, tier3.CHECKPOINT_PATH):
            if os.path.exists(path):
                out += open(path, encoding='utf-8').read()
        return out


class Tier3TargetRestrictions(Tier3ExecutorBase):
    """1-3, 22-24: what this executor will and will not be pointed at."""

    def test_1_a_wrong_test_id_is_rejected(self):
        with self.assertRaises(tier3.Halt):
            tier3.resolve_test('TIER3-TEST-99')

    def test_2_an_arbitrary_customer_id_is_rejected(self):
        """There is no argument that accepts a customer id, so the only way to
        target one is to edit this file - which is a reviewable act."""
        with self.assertRaises(tier3.Halt):
            tier3.resolve_test('220')
        mode, test_id, auth, commit = tier3.parse_argv(
            ['--execute', '220', '--customer', '999'])
        self.assertEqual(test_id, '220')
        with self.assertRaises(tier3.Halt):
            tier3.resolve_test(test_id)

    def test_3_the_bulk_manifest_is_rejected(self):
        for target in ('reports/phase10_customer_manifest.csv', 'ALL', '11849'):
            with self.assertRaises(tier3.Halt):
                tier3.resolve_test(target)

    def test_24_a_cohort_sized_definition_is_refused(self):
        oversized = tier3.Tier3Test(
            test_id='X', woo_ids=list(range(50)), expected_creates=50,
            expected_addresses=0, authorization_phrase='x', description='x',
            expected_metafield_keys=(), expected_phone_sent=False)
        with self.assertRaises(tier3.Halt):
            tier3.assert_not_bulk(oversized)

    def test_22_test_1_authorization_cannot_run_test_2(self):
        test2 = tier3.resolve_test('TIER3-TEST-2')
        with self.assertRaises(tier3.TestNotAuthorized):
            tier3.assert_test_authorization(
                test2, tier3.TESTS['TIER3-TEST-1'].authorization_phrase)

    def test_23_test_1_authorization_cannot_run_test_3(self):
        test3 = tier3.resolve_test('TIER3-TEST-3')
        with self.assertRaises(tier3.TestNotAuthorized):
            tier3.assert_test_authorization(
                test3, tier3.TESTS['TIER3-TEST-1'].authorization_phrase)

    def test_each_test_has_its_own_phrase(self):
        phrases = [t.authorization_phrase for t in tier3.TESTS.values()]
        self.assertEqual(len(phrases), len(set(phrases)))

    def test_test_3_cohort_is_frozen_and_still_needs_its_own_approval(self):
        """Frozen 2026-08-23 under Approval 2. Freezing the cohort settles WHO;
        it does not authorize the run."""
        d = tier3.resolve_test('TIER3-TEST-3')
        self.assertTrue(tier3.assert_cohort_frozen(d))
        with self.assertRaises(tier3.TestNotAuthorized):
            tier3.assert_test_authorization(d, 'looks fine')

    def test_test_3_records_woo_1_as_its_required_member(self):
        self.assertEqual(tier3.TIER3_TEST_3_REQUIRED_MEMBER, 1)
        self.assertIn('risk #45', tier3.TESTS['TIER3-TEST-3'].notes)


class Tier3PayloadCorrectness(Tier3ExecutorBase):
    """4-10: the payloads themselves."""

    def simulate_test_1(self):
        return tier3.simulate('TIER3-TEST-1',
                              candidate_loader=loader_for(tier3_candidate(220)))

    def simulate_test_2(self):
        return tier3.simulate(
            'TIER3-TEST-2',
            candidate_loader=loader_for(tier3_candidate(2, registered=False,
                                                        with_address=True)))

    def test_4_test_1_payload_is_correct(self):
        result = self.simulate_test_1()
        planned = result['planned'][0]
        self.assertEqual(planned['woo_customer_id'], 220)
        self.assertEqual(planned['customerCreate'], 1)
        self.assertEqual(sorted(planned['payload_fields']),
                         ['email', 'firstName', 'lastName', 'metafields', 'phone', 'tags'])

    def test_5_test_1_contains_no_address(self):
        result = self.simulate_test_1()
        self.assertEqual(result['planned_customerAddressCreate'], 0)
        self.assertEqual(result['planned'][0]['customerAddressCreate'], 0)

    def test_6_test_1_contains_no_consent(self):
        result = self.simulate_test_1()
        self.assertFalse(result['consent_on_any_payload'])
        self.assertNotIn('emailMarketingConsent', result['planned'][0]['payload_fields'])

    def test_7_test_1_contains_the_legacy_id(self):
        result = self.simulate_test_1()
        planned = result['planned'][0]
        self.assertIn(rt.LEGACY_KEY, planned['metafield_keys'])
        self.assertEqual(planned['legacy_id_value'], '220')
        self.assertEqual(planned['metafield_keys'],
                         [rt.LEGACY_KEY, rt.REGISTERED_AT_KEY])

    def test_8_test_2_contains_exactly_one_address(self):
        result = self.simulate_test_2()
        self.assertEqual(result['planned_customerAddressCreate'], 1)

    def test_9_gb_province_code_is_omitted(self):
        result = self.simulate_test_2()
        self.assertEqual(result['planned'][0]['province_code_sent'], [False])

    def test_10_postcode_is_unchanged(self):
        """build_plan halts if the zip is anything but the trimmed source."""
        candidate = tier3_candidate(2, registered=False, with_address=True)
        candidate['billing_zip'] = '  ' + SYNTHETIC_PII['zip'] + '  '
        stages = tier3.build_plan(tier3.resolve_test('TIER3-TEST-2'), candidate,
                                  phone_allowed=True)
        self.assertEqual(stages[1]['address']['zip'], SYNTHETIC_PII['zip'])

    def test_a_forbidden_field_halts_before_send(self):
        for field in ('addresses', 'emailMarketingConsent', 'password', 'username',
                      'company', 'wp_capabilities'):
            payload = {'email': 'a@example.com',
                       'metafields': [rt.legacy_metafield(220)], field: 'x'}
            with self.assertRaises(tier3.Halt, msg=field):
                tier3.assert_payload_contract(payload, 220)

    def test_an_undocumented_field_halts(self):
        payload = {'email': 'a@example.com', 'metafields': [rt.legacy_metafield(220)],
                   'loyaltyTier': 'gold'}
        with self.assertRaises(tier3.Halt):
            tier3.assert_payload_contract(payload, 220)

    def test_the_address_stage_requires_a_customer_id(self):
        result = self.simulate_test_2()
        self.assertEqual(result['planned'][0]['set_as_default'], [True])


class Tier3LiveGuards(Tier3ExecutorBase):
    """11-14, 17-21: the guards that stand between the executor and Shopify."""

    def run_test_1(self, mock, authorization=None, commit=None):
        return tier3.execute(
            'TIER3-TEST-1',
            authorization or tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
            commit or self.commit, mock, tier3.APPROVED_STORE_DOMAIN,
            tier3.EXPECTED_API_VERSION,
            candidate_loader=loader_for(tier3_candidate(220)),
            sleep=lambda _s: None, tree_check=lambda: True)

    def test_11_an_existing_legacy_id_halts(self):
        present = {'data': {'customers': {'edges': [{'node': {
            'id': 'gid://shopify/Customer/1',
            'metafield': {'value': '220'}}}]}}}
        mock = MockShopify(legacy=present)
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(mock)
        self.assertIn('already exists', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    def test_12_a_timeout_verifies_before_retrying(self):
        """The write may have landed. Ask before re-sending, or risk a duplicate."""
        state = {'calls': 0, 'created': False}

        def create(variables, _n):
            state['calls'] += 1
            if state['calls'] == 1:
                state['created'] = True          # it landed, then the socket died
                raise TimeoutError('connection reset')
            raise AssertionError('retried without acting on the verification')

        def legacy():
            if state['created']:
                return {'data': {'customers': {'edges': [{'node': {
                    'id': 'gid://shopify/Customer/5',
                    'metafield': {'value': '220'}}}]}}}
            return NO_LEGACY_MATCH

        mock = MockShopify(legacy=legacy, create_behaviour=create)
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(mock)
        self.assertIn('verified as having landed', str(caught.exception))
        self.assertEqual(state['calls'], 1)

    def test_13_a_401_halts_and_is_never_retried(self):
        def create(_variables, _n):
            raise RuntimeError('HTTP 401 Unauthorized')

        mock = MockShopify(create_behaviour=create)
        with self.assertRaises(rt.HaltMigration):
            self.run_test_1(mock)

    def test_13b_access_denied_in_the_preflight_halts(self):
        denied = {'errors': [{'message': 'denied',
                              'extensions': {'code': 'ACCESS_DENIED'}}]}
        mock = MockShopify(preflight=denied)
        with self.assertRaises(tier3.Halt):
            self.run_test_1(mock)
        self.assertEqual(mock.mutations_sent, 0)

    def test_14_throttling_uses_the_existing_runtime(self):
        """No second throttle implementation: the executor imports the tested one."""
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        # The loop is local because the runtime refuses mutation documents by
        # construction. The POLICY is entirely the runtime's: no schedule, no
        # jitter and no classification is defined in the executor.
        self.assertNotIn('class ThrottleController', source)
        self.assertNotIn('BACKOFF_SCHEDULE =', source)
        self.assertIn('rt.ThrottleController()', source)
        self.assertIn('rt.backoff_delay(', source)
        self.assertIn('rt.classify_response(', source)
        self.assertIn('rt.classify_exception(', source)
        self.assertIn('rt.MAX_TRANSIENT_ATTEMPTS', source)
        self.assertEqual(rt.BACKOFF_SCHEDULE, (1, 2, 4, 8, 16))
        with self.assertRaises(rt.HaltMigration):
            rt.execute_with_retry(lambda d, v: {}, tier3.CUSTOMER_CREATE)

    def test_17_a_production_store_is_rejected(self):
        production = json.loads(json.dumps(PREFLIGHT_OK))
        production['data']['shop']['plan'] = {'displayName': 'Shopify Plus',
                                              'partnerDevelopment': False}
        mock = MockShopify(preflight=production)
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(mock)
        self.assertIn('development store', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    def test_18_missing_partner_development_is_treated_as_unsafe(self):
        unknown = json.loads(json.dumps(PREFLIGHT_OK))
        unknown['data']['shop']['plan'] = {'displayName': 'Grow'}
        mock = MockShopify(preflight=unknown)
        with self.assertRaises(tier3.Halt):
            self.run_test_1(mock)
        self.assertEqual(mock.mutations_sent, 0)

    def test_19_schema_drift_halts(self):
        drifted = json.loads(json.dumps(PREFLIGHT_OK))
        drifted['data']['__type']['inputFields'].append({'name': 'addresses'})
        mock = MockShopify(preflight=drifted)
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(mock)
        self.assertIn('schema drift', str(caught.exception))

    def test_19b_a_missing_scope_halts(self):
        thin = json.loads(json.dumps(PREFLIGHT_OK))
        thin['data']['currentAppInstallation']['accessScopes'] = [
            {'handle': 'read_customers'}]
        with self.assertRaises(tier3.Halt):
            self.run_test_1(MockShopify(preflight=thin))

    def test_19c_a_nonempty_store_halts_test_1(self):
        occupied = json.loads(json.dumps(PREFLIGHT_OK))
        occupied['data']['customersCount']['count'] = 3
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(MockShopify(preflight=occupied))
        self.assertIn('expects exactly 0', str(caught.exception))

    def test_19d_an_unexpected_api_version_halts(self):
        with self.assertRaises(tier3.Halt):
            tier3.execute('TIER3-TEST-1',
                          tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
                          self.commit, MockShopify(), tier3.APPROVED_STORE_DOMAIN,
                          '2024-01',
                          candidate_loader=loader_for(tier3_candidate(220)),
                          tree_check=lambda: True)

    def test_19e_the_wrong_store_halts(self):
        with self.assertRaises(tier3.Halt):
            tier3.execute('TIER3-TEST-1',
                          tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
                          self.commit, MockShopify(), 'someone-else.myshopify.com',
                          tier3.EXPECTED_API_VERSION,
                          candidate_loader=loader_for(tier3_candidate(220)),
                          tree_check=lambda: True)

    def test_20_a_wrong_contract_hash_halts(self):
        with self.assertRaises(tier3.Halt) as caught:
            tier3.assert_contract_unchanged(expected='0' * 64)
        self.assertIn('contract hash mismatch', str(caught.exception))

    def test_20b_the_frozen_contract_hash_matches_today(self):
        self.assertEqual(tier3.assert_contract_unchanged(), tier3.CONTRACT_SHA256)

    def test_21_a_wrong_commit_halts(self):
        mock = MockShopify()
        with self.assertRaises(tier3.Halt) as caught:
            self.run_test_1(mock, commit='deadbeefdeadbeef')
        self.assertIn('commit mismatch', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    def test_21b_a_missing_commit_halts(self):
        with self.assertRaises(tier3.Halt):
            tier3.assert_expected_commit(None)

    def test_authorization_is_checked_before_the_store_is_touched(self):
        mock = MockShopify()
        with self.assertRaises(tier3.TestNotAuthorized):
            self.run_test_1(mock, authorization='please')
        self.assertEqual(mock.documents_sent, [])


class Tier3AuditSafety(Tier3ExecutorBase):
    """15-16: what reaches the audit trail."""

    def successful_run(self):
        mock = MockShopify()
        result = tier3.execute(
            'TIER3-TEST-1', tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
            self.commit, mock, tier3.APPROVED_STORE_DOMAIN,
            tier3.EXPECTED_API_VERSION,
            candidate_loader=loader_for(tier3_candidate(220)),
            sleep=lambda _s: None, tree_check=lambda: True)
        return mock, result

    def test_15_the_token_is_never_logged(self):
        _mock, _result = self.successful_run()
        self.assertNotIn(FAKE_TOKEN, self.ledger_text())
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        for leak in ("print(token", "print(config['token']", 'log(token'):
            self.assertNotIn(leak, source)

    def test_16_no_pii_reaches_the_audit_trail(self):
        _mock, _result = self.successful_run()
        text = self.ledger_text()
        self.assertTrue(text)
        for value in SYNTHETIC_PII.values():
            self.assertNotIn(value, text)
        self.assertIn('220', text)

    def test_16b_user_errors_are_sanitized_before_being_written(self):
        def create(variables, _n):
            return {'data': {'customerCreate': {'customer': None, 'userErrors': [
                {'field': ['email'],
                 'message': f"Email {SYNTHETIC_PII['email']} is invalid"}]}}}

        mock = MockShopify(create_behaviour=create)
        tier3.execute('TIER3-TEST-1',
                      tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
                      self.commit, mock, tier3.APPROVED_STORE_DOMAIN,
                      tier3.EXPECTED_API_VERSION,
                      candidate_loader=loader_for(tier3_candidate(220)),
                      sleep=lambda _s: None, tree_check=lambda: True)
        text = self.ledger_text()
        self.assertNotIn(SYNTHETIC_PII['email'], text)
        self.assertIn('EMAIL_REDACTED', text)

    def test_the_ledger_schema_permits_only_approved_identifiers(self):
        _mock, _result = self.successful_run()
        for line in self.ledger_text().splitlines():
            record = json.loads(line)
            if 'woo_id' in record:      # checkpoint line
                self.assertEqual(set(record), {'woo_id', 'gid', 'action'})
            else:
                rt.assert_ledger_record_safe(record)

    def test_a_successful_run_records_the_gid_and_the_commit(self):
        _mock, result = self.successful_run()
        self.assertEqual(result['mutations']['customerCreate'], 1)
        self.assertEqual(result['mutations']['customerAddressCreate'], 0)
        self.assertEqual(len(result['created']), 1)
        self.assertTrue(result['created'][0]['gid'].startswith('gid://shopify/Customer/'))
        self.assertEqual(result['executor_commit'], tier3.reviewed_commit())

    def test_the_result_records_the_enforced_commit_not_head(self):
        """The Test-1 auditability defect. The result used to stamp HEAD, so an
        evidence file could name a commit that decides nothing - at Test 1, HEAD
        was ec2e61d while the guard enforced 7987aa7. The two are now recorded
        separately and executor_commit is the one that was enforced."""
        _mock, result = self.successful_run()
        self.assertEqual(result['executor_commit'], tier3.reviewed_commit())
        self.assertEqual(result['head_commit'], tier3.git_head())
        self.assertEqual(len(result['executor_commit']), 40)

    def test_executor_commit_differs_from_head_when_docs_moved_on(self):
        """Only meaningful while HEAD has advanced past the behavioural commit -
        which is exactly the situation that produced the defect."""
        if tier3.reviewed_commit() == tier3.git_head():
            self.skipTest('HEAD is the behavioural commit; nothing to distinguish')
        _mock, result = self.successful_run()
        self.assertNotEqual(result['executor_commit'], result['head_commit'])

    def test_the_audit_ledger_stamps_the_enforced_commit(self):
        _mock, _result = self.successful_run()
        for line in self.ledger_text().splitlines():
            record = json.loads(line)
            if 'importer_commit' in record:
                self.assertEqual(record['importer_commit'], tier3.reviewed_commit())

    def test_the_simulation_result_records_both_commits(self):
        result = tier3.simulate('TIER3-TEST-1',
                                candidate_loader=loader_for(tier3_candidate(220)))
        self.assertEqual(result['executor_commit'], tier3.reviewed_commit())
        self.assertEqual(result['head_commit'], tier3.git_head())

    def test_a_docs_only_commit_does_not_invalidate_an_approval(self):
        """reviewed_commit() tracks the files that decide behaviour, not HEAD."""
        self.assertIn('phase10_tier3_executor.py', tier3.BEHAVIOUR_PATHS[0])
        self.assertNotIn('docs/', ' '.join(tier3.BEHAVIOUR_PATHS))
        self.assertEqual(len(tier3.reviewed_commit()), 40)


class Tier3RollbackIsNotExecutable(Tier3ExecutorBase):
    """Rollback is a mutation, and Approval A did not grant it."""

    def test_the_delete_document_exists_but_has_no_caller(self):
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        self.assertIn('customerDelete', tier3.CUSTOMER_DELETE)
        self.assertEqual(source.count('CUSTOMER_DELETE'), 1)

    def test_execute_rollback_raises(self):
        with self.assertRaises(tier3.RollbackNotAuthorized):
            tier3.execute_rollback([{'woo_customer_id': 220, 'gid': 'gid://x'}])

    def test_the_rollback_flag_is_false(self):
        self.assertFalse(tier3.ROLLBACK_AUTHORIZED)

    def test_the_spec_describes_without_doing(self):
        spec = tier3.rollback_spec([{'woo_customer_id': 220, 'gid': 'gid://x'}])
        self.assertFalse(spec['executable'])
        self.assertTrue(spec['document_present'])
        self.assertIn('separate explicit authorization', spec['authorization_required'])


class Tier3NoNetworkWrites(Tier3ExecutorBase):
    """25 + the critical no-write proof."""

    SOURCE = None

    def setUp(self):
        super().setUp()
        self.SOURCE = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                           encoding='utf-8').read()

    def test_25_transport_is_never_imported_at_module_level(self):
        """The import lives inside main(), so a simulation cannot reach the
        network even by accident."""
        for line in self.SOURCE.splitlines():
            if 'from phase9_preflight import' in line:
                self.assertTrue(line.startswith('    '),
                                'transport must only be imported inside main()')
        self.assertNotIn('\nimport urllib', self.SOURCE)
        self.assertNotIn('\nimport requests', self.SOURCE)

    def test_25b_execute_requires_injected_transport(self):
        import inspect
        self.assertIn('send', inspect.signature(tier3.execute).parameters)

    def test_25c_simulation_sends_nothing(self):
        result = tier3.simulate('TIER3-TEST-1',
                                candidate_loader=loader_for(tier3_candidate(220)))
        self.assertEqual(result['shopify_mutations_performed'], 0)
        self.assertEqual(result['customer_writes'], 0)
        self.assertEqual(result['address_writes'], 0)
        self.assertEqual(result['metafield_writes'], 0)

    def test_25d_the_forbidden_mutations_are_absent(self):
        for forbidden in ('customerSet', 'bulkOperationRunMutation', 'customerUpdate',
                          'metafieldsSet', 'customerAddressDelete',
                          'inventorySetQuantities', 'productCreate', 'orderCreate',
                          'collectionCreate'):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_25e_only_three_mutation_documents_exist(self):
        documents = [name for name in dir(tier3)
                     if isinstance(getattr(tier3, name), str)
                     and getattr(tier3, name).strip().startswith('mutation')]
        self.assertEqual(sorted(documents),
                         ['CUSTOMER_ADDRESS_CREATE', 'CUSTOMER_CREATE',
                          'CUSTOMER_DELETE'])

    def test_25f_default_mode_is_simulation(self):
        self.assertEqual(tier3.DEFAULT_MODE, tier3.MODE_SIMULATE)
        mode, test_id, _a, _c = tier3.parse_argv(['--simulate', 'TIER3-TEST-1'])
        self.assertEqual(mode, tier3.MODE_SIMULATE)


# ---------------------------------------------------------------------------
# Test-2 store-state guard (Approval A, 2026-08-23)
# ---------------------------------------------------------------------------
#
# The amendment replaced an inherited boolean (`requires_store_empty`) with an
# explicit two-part invariant: an EXACT expected customer count, and the
# IDENTITY of whoever must already be there. Both run before any mutation.
#
# Every test here is offline. The store is a dict.

TEST1_GID = 'gid://shopify/Customer/10159741272320'


def preflight_response(count, scopes=('read_customers', 'write_customers')):
    return {'data': {
        'shop': {'name': 'Wholesale Beautyhub',
                 'myshopifyDomain': tier3.APPROVED_STORE_DOMAIN,
                 'plan': {'displayName': 'Grow', 'partnerDevelopment': True}},
        'currentAppInstallation': {'accessScopes': [{'handle': h} for h in scopes]},
        'customersCount': {'count': count},
        '__type': {'inputFields': [{'name': n} for n in
                                   ('email', 'firstName', 'lastName', 'phone',
                                    'tags', 'metafields')]},
    }}


def legacy_hit(woo_id, gid):
    return {'data': {'customers': {'edges': [
        {'node': {'id': gid, 'metafield': {'value': str(woo_id)}}}]}}}


LEGACY_MISS = {'data': {'customers': {'edges': []}}}


class StoreMock:
    """Answers the pre-flight and per-id legacy lookups. Never a network."""

    def __init__(self, count, present=None):
        self.count = count
        self.present = dict(present or {})   # {woo_id: gid}
        self.sent = []
        self.mutations_sent = 0

    def __call__(self, document, variables=None):
        self.sent.append(document)
        if document.strip().startswith('mutation'):
            self.mutations_sent += 1
            raise AssertionError('a mutation was sent while the state guard was '
                                 'supposed to have halted')
        if 'shop {' in document:
            return preflight_response(self.count)
        m = re.search(r"legacy_woo_customer_id:'(\d+)'", document)
        if m:
            woo = int(m.group(1))
            gid = self.present.get(woo)
            return legacy_hit(woo, gid) if gid else LEGACY_MISS
        raise AssertionError(f'unexpected document: {document[:60]}')


class Tier3StoreStateGuard(Tier3ExecutorBase):

    def preflight_test2(self, mock):
        return tier3.preflight(mock, tier3.TESTS['TIER3-TEST-2'],
                               tier3.APPROVED_STORE_DOMAIN,
                               tier3.EXPECTED_API_VERSION)

    # ---- A: an empty store is now WRONG for Test 2 -----------------------
    def test_A_count_zero_halts(self):
        """Test 2 runs after Test 1. An empty store means Test 1's customer is
        gone, which is a state nobody approved."""
        mock = StoreMock(count=0)
        with self.assertRaises(tier3.Halt) as caught:
            self.preflight_test2(mock)
        self.assertIn('expects exactly 1', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    # ---- B: exactly one, and it is the Test-1 customer -------------------
    def test_B_count_one_with_the_test1_customer_passes_the_state_guard(self):
        mock = StoreMock(count=1, present={220: TEST1_GID})
        state = self.preflight_test2(mock)
        self.assertEqual(state['customers_before'], 1)
        self.assertEqual(state['expected_customer_count'], 1)
        self.assertEqual(state['expected_preexisting_woo_ids'], [220])
        self.assertEqual(mock.mutations_sent, 0)

    # ---- C / D: too many ------------------------------------------------
    def test_C_count_two_halts(self):
        mock = StoreMock(count=2, present={220: TEST1_GID})
        with self.assertRaises(tier3.Halt) as caught:
            self.preflight_test2(mock)
        self.assertIn('store holds 2', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    def test_D_count_three_or_more_halts(self):
        for count in (3, 10, 11849):
            mock = StoreMock(count=count, present={220: TEST1_GID})
            with self.assertRaises(tier3.Halt):
                self.preflight_test2(mock)
            self.assertEqual(mock.mutations_sent, 0)

    # ---- E: an unexpected customer --------------------------------------
    def test_E_an_unexpected_customer_halts(self):
        """One customer, but not the one that should be there. The count check
        passes; only the identity check catches this."""
        mock = StoreMock(count=1, present={999: 'gid://shopify/Customer/1'})
        with self.assertRaises(tier3.Halt) as caught:
            self.preflight_test2(mock)
        self.assertIn('expects woo_customer_id=220 to already exist',
                      str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    # ---- F: Test-1 customer present but something else appeared ---------
    def test_F_test1_customer_plus_another_halts(self):
        mock = StoreMock(count=2, present={220: TEST1_GID, 999: 'gid://shopify/Customer/2'})
        with self.assertRaises(tier3.Halt) as caught:
            self.preflight_test2(mock)
        self.assertIn('store holds 2', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    # ---- G: right count, identity unverifiable --------------------------
    def test_G_correct_count_but_test1_customer_unverifiable_halts(self):
        """The case that makes the identity check worth having: the count is
        exactly right and the store is still wrong."""
        mock = StoreMock(count=1, present={})
        with self.assertRaises(tier3.Halt) as caught:
            self.preflight_test2(mock)
        self.assertIn('does not', str(caught.exception))
        self.assertEqual(mock.mutations_sent, 0)

    # ---- H: correct count, verified identity, proceed -------------------
    def test_H_correct_state_proceeds_to_the_remaining_checks(self):
        mock = StoreMock(count=1, present={220: TEST1_GID})
        state = self.preflight_test2(mock)
        self.assertTrue(state['development_store'])
        self.assertEqual(state['scopes'], 2)
        self.assertEqual(state['api_version'], tier3.EXPECTED_API_VERSION)
        # the identity lookup really was issued, not skipped
        self.assertTrue(any("legacy_woo_customer_id:'220'" in d for d in mock.sent))
        self.assertEqual(mock.mutations_sent, 0)

    # ---- the amendment did not weaken anything --------------------------
    def test_the_guard_is_stricter_than_disabling_the_check(self):
        """`requires_store_empty=False` would have accepted any store at all.
        The replacement accepts exactly one state."""
        for count in (0, 2, 3):
            with self.assertRaises(tier3.Halt):
                self.preflight_test2(StoreMock(count=count, present={220: TEST1_GID}))

    def test_test_1_still_requires_an_empty_store(self):
        d = tier3.TESTS['TIER3-TEST-1']
        self.assertEqual(d.expected_customer_count, 0)
        self.assertEqual(d.expected_preexisting_woo_ids, ())
        mock = StoreMock(count=1, present={220: TEST1_GID})
        with self.assertRaises(tier3.Halt) as caught:
            tier3.preflight(mock, d, tier3.APPROVED_STORE_DOMAIN,
                            tier3.EXPECTED_API_VERSION)
        self.assertIn('expects exactly 0', str(caught.exception))

    def test_test_3_expects_the_two_earlier_test_customers(self):
        """Once the cohort was frozen the prior population became statable, so
        Test 3 got the same exact invariant as Test 2 rather than keeping the
        'assert nothing' placeholder."""
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertEqual(d.expected_customer_count, 2)
        self.assertEqual(sorted(d.expected_preexisting_woo_ids), [2, 220])
        good = StoreMock(count=2, present={220: TEST1_GID,
                                           2: 'gid://shopify/Customer/10160661102848'})
        state = tier3.preflight(good, d, tier3.APPROVED_STORE_DOMAIN,
                                tier3.EXPECTED_API_VERSION)
        self.assertEqual(state['customers_before'], 2)
        for count in (0, 1, 3):
            with self.assertRaises(tier3.Halt):
                tier3.preflight(StoreMock(count=count, present={220: TEST1_GID}),
                                d, tier3.APPROVED_STORE_DOMAIN,
                                tier3.EXPECTED_API_VERSION)

    def test_the_old_boolean_is_gone(self):
        """No code path reads the old flag. The name still appears in one
        comment explaining why it was replaced, which is documentation worth
        keeping - so this asserts the absence of the parameter and the
        attribute, not the absence of the word."""
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        import inspect
        params = inspect.signature(tier3.Tier3Test.__init__).parameters
        self.assertNotIn('requires_store_empty', params)
        self.assertIn('expected_customer_count', params)
        self.assertIn('expected_preexisting_woo_ids', params)
        # code-only forms; these cannot appear in explanatory prose
        self.assertNotIn('self.requires_store_empty', source)
        self.assertNotIn('definition.requires_store_empty', source)
        for test in tier3.TESTS.values():
            self.assertFalse(hasattr(test, 'requires_store_empty'))
            self.assertTrue(hasattr(test, 'expected_customer_count'))

    def test_one_lookup_implementation_serves_both_checks(self):
        """Absence and presence share find_customer_by_legacy_id. Two copies
        would be two things that could drift."""
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        self.assertEqual(source.count('LEGACY_LOOKUP %'), 1)
        self.assertIn('find_customer_by_legacy_id', source)

    def test_the_state_guard_runs_before_any_mutation(self):
        """End to end with mocks: a bad state sends zero mutations even though
        the authorization and commit are both valid."""
        mock = StoreMock(count=0)
        with self.assertRaises(tier3.Halt):
            tier3.execute('TIER3-TEST-2',
                          tier3.TESTS['TIER3-TEST-2'].authorization_phrase,
                          tier3.reviewed_commit(), mock,
                          tier3.APPROVED_STORE_DOMAIN, tier3.EXPECTED_API_VERSION,
                          candidate_loader=loader_for(
                              tier3_candidate(2, registered=False, with_address=True)),
                          sleep=lambda _s: None, tree_check=lambda: True)
        self.assertEqual(mock.mutations_sent, 0)


class Tier3Test2PayloadUnchanged(Tier3ExecutorBase):
    """The store-state guard is the only behavioural change. The payload the
    test would send must be exactly what was approved."""

    def test_the_definition_is_otherwise_untouched(self):
        d = tier3.TESTS['TIER3-TEST-2']
        self.assertEqual(list(d.woo_ids), [2])
        self.assertEqual(d.expected_creates, 1)
        self.assertEqual(d.expected_addresses, 1)
        self.assertEqual(d.expected_metafield_keys, (rt.LEGACY_KEY,))
        self.assertTrue(d.expected_phone_sent)
        self.assertEqual(d.expected_country, 'GB')
        self.assertTrue(d.province_must_be_omitted)
        self.assertEqual(d.authorization_phrase,
                         'APPROVED - EXECUTE TIER-3 TEST 2 FOR WOO CUSTOMER 2')

    def test_the_payload_shape_is_unchanged(self):
        result = tier3.simulate(
            'TIER3-TEST-2',
            candidate_loader=loader_for(tier3_candidate(2, registered=False,
                                                        with_address=True)))
        planned = result['planned'][0]
        self.assertEqual(planned['customerCreate'], 1)
        self.assertEqual(planned['customerAddressCreate'], 1)
        self.assertEqual(planned['metafield_keys'], [rt.LEGACY_KEY])
        self.assertEqual(planned['legacy_id_value'], '2')
        self.assertTrue(planned['phone_sent'])
        self.assertFalse(planned['consent_present'])
        self.assertEqual(planned['province_code_sent'], [False])
        self.assertEqual(planned['set_as_default'], [True])
        self.assertEqual(sorted(planned['payload_fields']),
                         ['email', 'firstName', 'lastName', 'metafields',
                          'phone', 'tags'])


# ---------------------------------------------------------------------------
# Tier-3 executor: phone fallback, source-backed loader, frozen Test-3 cohort
# ---------------------------------------------------------------------------


def phone_error_then_success(woo_id):
    """Reject the first create on the phone, accept the retry. Mirrors what
    Shopify did to woo_customer_id=1 in the Gate 6 run."""
    state = {'n': 0}

    def behave(variables, _seq):
        state['n'] += 1
        sent = variables['input']
        if state['n'] == 1:
            assert 'phone' in sent, 'the first attempt should carry the phone'
            return {'data': {'customerCreate': {'customer': None, 'userErrors': [
                {'field': ['phone'], 'message': 'Phone is invalid'}]}}}
        assert 'phone' not in sent, 'the retry must not carry the phone'
        return created_customer(variables, woo_id)
    return behave


class Tier3PhoneFallback(Tier3ExecutorBase):
    """Risk #45 in the Tier-3 executor. Before this, a phone Shopify rejected
    cost the whole customer - the failure the fix exists to prevent."""

    def setUp(self):
        super().setUp()
        self._dropped = os.path.join(self._dir.name, 'dropped.jsonl')
        self._orig = rt.DROPPED_PHONES_PATH
        rt.DROPPED_PHONES_PATH = self._dropped
        self.addCleanup(setattr, rt, 'DROPPED_PHONES_PATH', self._orig)

    def run_test_1(self, mock):
        return tier3.execute(
            'TIER3-TEST-1', tier3.TESTS['TIER3-TEST-1'].authorization_phrase,
            tier3.reviewed_commit(), mock, tier3.APPROVED_STORE_DOMAIN,
            tier3.EXPECTED_API_VERSION,
            candidate_loader=loader_for(tier3_candidate(220)),
            sleep=lambda _s: None, tree_check=lambda: True)

    def test_a_rejected_phone_no_longer_costs_the_customer(self):
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        result = self.run_test_1(mock)
        self.assertEqual(len(result['created']), 1)
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['phone_fallbacks'], [220])
        self.assertEqual(result['customers_saved_by_phone_fallback'], [220])

    def test_the_fallback_costs_a_second_create_call(self):
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        result = self.run_test_1(mock)
        self.assertEqual(result['mutations']['customerCreate'], 2)
        self.assertEqual(len(result['created']), 1)

    def test_the_cap_is_on_customers_created_not_calls(self):
        """Counting calls would halt a correct run AFTER the writes landed."""
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        result = self.run_test_1(mock)   # 2 calls, 1 customer, expected_creates=1
        self.assertEqual(len(result['created']),
                         tier3.TESTS['TIER3-TEST-1'].expected_creates)

    def test_the_drop_is_tagged_and_logged(self):
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        self.run_test_1(mock)
        retry = mock.inputs[1]
        self.assertIn(rt.TAG_PHONE_DROPPED, retry['tags'])
        self.assertTrue(os.path.exists(self._dropped))
        events = [json.loads(l) for l in
                  open(self._dropped, encoding='utf-8').read().splitlines() if l.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['woo_customer_id'], 220)
        self.assertEqual(events[0]['reason'], rt.PHONE_DROP_INVALID)

    def test_the_legacy_metafield_survives_the_retry(self):
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        self.run_test_1(mock)
        retry = mock.inputs[1]
        self.assertEqual([m['value'] for m in retry['metafields']
                          if m['key'] == rt.LEGACY_KEY], ['220'])

    def test_the_ledger_records_the_drop(self):
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        self.run_test_1(mock)
        statuses = [json.loads(l)['status'] for l in self.ledger_text().splitlines()
                    if l.strip() and 'status' in l]
        self.assertIn('PHONE_DROPPED_RETRYING', statuses)
        self.assertIn('CREATED_PHONE_DROPPED', statuses)

    def test_the_retry_happens_once_and_only_once(self):
        def always_phone_error(_variables, _seq):
            return {'data': {'customerCreate': {'customer': None, 'userErrors': [
                {'field': ['phone'], 'message': 'Phone is invalid'}]}}}
        mock = MockShopify(create_behaviour=always_phone_error)
        result = self.run_test_1(mock)
        self.assertEqual(result['mutations']['customerCreate'], 2)
        self.assertEqual(result['failed'], [220])
        self.assertEqual(result['created'], [])

    def test_a_non_phone_error_is_not_retried(self):
        def email_error(_variables, _seq):
            return {'data': {'customerCreate': {'customer': None, 'userErrors': [
                {'field': ['email'], 'message': 'Email is invalid'}]}}}
        mock = MockShopify(create_behaviour=email_error)
        result = self.run_test_1(mock)
        self.assertEqual(result['mutations']['customerCreate'], 1)
        self.assertEqual(result['phone_fallbacks'], [])

    def test_a_clean_create_neither_retries_nor_logs(self):
        mock = MockShopify()
        result = self.run_test_1(mock)
        self.assertEqual(result['mutations']['customerCreate'], 1)
        self.assertEqual(result['phone_fallbacks'], [])
        self.assertFalse(os.path.exists(self._dropped))

    def test_the_test_suite_cannot_write_to_the_real_dropped_phone_log(self):
        """Regression. rt.phone_fallback's log_path default binds at definition
        time, so rebinding rt.DROPPED_PHONES_PATH in setUp did nothing and 31
        fabricated entries reached the real audit log. The executor now reads
        the attribute at call time, which is what makes the redirect work."""
        real = os.path.join(REPO_ROOT, 'reports', 'phase10_dropped_phones.jsonl')
        before = os.path.getsize(real) if os.path.exists(real) else 0
        mock = MockShopify(create_behaviour=phone_error_then_success(220))
        self.run_test_1(mock)
        after = os.path.getsize(real) if os.path.exists(real) else 0
        self.assertEqual(before, after,
                         'the test suite wrote to the real dropped-phone log')
        self.assertTrue(os.path.exists(self._dropped),
                        'the redirect did not take effect')

    def test_the_executor_resolves_the_log_path_at_call_time(self):
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        self.assertIn('log_path=rt.DROPPED_PHONES_PATH', source)


class Tier3PerCustomerExpectations(Tier3ExecutorBase):
    """A ten-customer cohort has ten shapes; flat expectations cannot describe
    it, and loosening them to fit would be worse than describing them."""

    def test_flat_expectations_still_apply_when_no_override_exists(self):
        e = tier3.expectations_for(tier3.TESTS['TIER3-TEST-1'], 220)
        self.assertEqual(e['addresses'], 0)
        self.assertEqual(e['metafields'], (rt.LEGACY_KEY, rt.REGISTERED_AT_KEY))
        self.assertTrue(e['phone_sent'])

    def test_per_customer_overrides_the_flat_fields(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertEqual(tier3.expectations_for(d, 227)['addresses'], 0)
        self.assertEqual(tier3.expectations_for(d, 17)['phone_sent'], False)
        self.assertEqual(tier3.expectations_for(d, 70)['country'], 'IE')
        self.assertFalse(tier3.expectations_for(d, 70)['province_omitted'])
        self.assertTrue(tier3.expectations_for(d, 1)['province_omitted'])

    def test_execute_uses_the_per_customer_phone_expectation(self):
        """Regression: execute() read the FLAT expected_phone_sent, which is None
        for a mixed cohort, so bool(None) omitted every phone. The per-customer
        guard halted it before any mutation - this pins the fix."""
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        self.assertNotIn('phone_allowed=bool(definition.expected_phone_sent)', source)
        self.assertIn("expectations_for(definition, woo_id)['phone_sent']", source)

    def test_a_mismatch_against_the_per_customer_expectation_halts(self):
        d = tier3.TESTS['TIER3-TEST-3']
        # woo 227 must plan zero addresses; hand it one and the plan must refuse
        with self.assertRaises(tier3.Halt):
            tier3.build_plan(d, tier3_candidate(227, registered=True,
                                                with_address=True),
                             phone_allowed=False)


class Tier3FrozenCohort(Tier3ExecutorBase):

    COHORT = [1, 17, 957, 3, 227, 4, 217, 6, 70, 1669]

    def test_the_cohort_is_frozen(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertTrue(d.cohort_frozen)
        self.assertEqual(list(d.woo_ids), self.COHORT)
        self.assertTrue(tier3.assert_cohort_frozen(d))

    def test_exactly_ten_unique_ids_including_woo_1(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertEqual(len(d.woo_ids), 10)
        self.assertEqual(len(set(d.woo_ids)), 10)
        self.assertIn(tier3.TIER3_TEST_3_REQUIRED_MEMBER, d.woo_ids)
        self.assertTrue(tier3.assert_no_duplicate_ids(d))

    def test_no_test_1_or_test_2_customer_is_reused(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertNotIn(220, d.woo_ids)
        self.assertNotIn(2, d.woo_ids)

    def test_the_totals_reconcile(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertEqual(d.expected_creates, 10)
        self.assertEqual(d.expected_addresses, 9)
        self.assertEqual(sum(e['addresses'] for e in d.per_customer.values()), 9)
        self.assertEqual(len(d.per_customer), 10)

    def test_it_expects_the_test_1_and_test_2_customers_to_be_present(self):
        d = tier3.TESTS['TIER3-TEST-3']
        self.assertEqual(d.expected_customer_count, 2)
        self.assertEqual(sorted(d.expected_preexisting_woo_ids), [2, 220])

    def test_it_is_still_bounded_at_ten(self):
        self.assertTrue(tier3.assert_not_bulk(tier3.TESTS['TIER3-TEST-3']))
        self.assertEqual(len(tier3.TESTS['TIER3-TEST-3'].woo_ids),
                         tier3.TIER3_MAX_CUSTOMERS)

    def test_its_authorization_is_still_its_own(self):
        d = tier3.TESTS['TIER3-TEST-3']
        for other in ('TIER3-TEST-1', 'TIER3-TEST-2'):
            with self.assertRaises(tier3.TestNotAuthorized):
                tier3.assert_test_authorization(
                    d, tier3.TESTS[other].authorization_phrase)


class Tier3SourceBackedLoader(Tier3ExecutorBase):
    """The manifest says who is approved; the source supplies the fields. It
    carries no shipping postcode or province, which is why a shipping-fallback
    member cannot be built from it."""

    def test_the_manifest_lacks_the_shipping_fields(self):
        import csv
        path = os.path.join(REPO_ROOT, tier3.MANIFEST_PATH)
        if not os.path.exists(path):
            self.skipTest('manifest not present')
        header = next(csv.reader(open(path, encoding='utf-8')))
        self.assertNotIn('shipping_zip', header)
        self.assertNotIn('shipping_province', header)

    def test_both_entry_points_default_to_the_source_backed_loader(self):
        import inspect
        for fn in (tier3.simulate, tier3.execute):
            default = inspect.signature(fn).parameters['candidate_loader'].default
            self.assertIs(default, tier3.load_approved_candidate)

    def test_the_loader_checks_the_manifest_first(self):
        source = open(os.path.join(SCRIPTS, 'phase10_tier3_executor.py'),
                      encoding='utf-8').read()
        self.assertIn('load_manifest_candidate(woo_id, path, expected_hash)', source)
        self.assertIn('disagree on identity', source)


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
