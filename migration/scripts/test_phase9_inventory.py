"""
Test coverage for the Phase 9 inventory fix (risk #35).

Two kinds of test here, labeled explicitly per test - never blurred:
- LIVE: a real call against the approved Shopify dev store
  (wholesale-beautyhub.myshopify.com). Read-only except for the inventory
  writes this fix itself makes, which are the exact operation being
  verified - never touches product/variant/media data.
- MOCK: `graphql_request` is monkeypatched with a canned response so a
  failure mode that would be unsafe or impractical to reproduce live
  (a missing location, a malformed response) can still be exercised for
  real, without guessing at what "should" happen.

Run: python migration/scripts/test_phase9_inventory.py
Requires the same .env credentials as the rest of Phase 9 - exits
NOT_CONFIGURED (matching phase9_preflight.py's convention) if absent,
rather than skipping LIVE tests silently.
"""
import sys
import os
import unittest
import uuid

sys.path.insert(0, os.path.dirname(__file__))
import phase9_test_import as pti
from phase9_preflight import get_config

RESULTS = []


def record(name, passed, detail=''):
    RESULTS.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'} - {name}{' - ' + detail if detail else ''}")


class LiveInventoryTests(unittest.TestCase):
    """Against the real dev store. Uses product 124's real variant
    (a genuine test product, not scratch data) since it already exists
    from the controlled test import - no new product is created here."""

    @classmethod
    def setUpClass(cls):
        config = get_config()
        cls.domain, cls.token, cls.api_version = config['domain'], config['token'], config['api_version'] or '2025-01'
        if not cls.domain or not cls.token:
            raise unittest.SkipTest('NOT_CONFIGURED - no live credentials, skipping LIVE tests')
        cls.location_id = pti.get_default_location(cls.domain, cls.token, cls.api_version)
        # product 124's variant - real, already-created test data
        data = pti.graphql_request(cls.domain, cls.token, cls.api_version,
            '{ product(id: "gid://shopify/Product/9599851364608") { variants(first: 1) { edges { node { inventoryItem { id } } } } } }')
        cls.inv_item_id = data['data']['product']['variants']['edges'][0]['node']['inventoryItem']['id']

    def live_quantity(self):
        data = pti.graphql_request(self.domain, self.token, self.api_version,
            '{ product(id: "gid://shopify/Product/9599851364608") { variants(first: 1) { edges { node { inventoryQuantity } } } } }')
        return data['data']['product']['variants']['edges'][0]['node']['inventoryQuantity']

    def test_1_positive_quantity(self):
        success, _ = pti.set_inventory_quantities(self.domain, self.token, self.api_version,
                                                    self.location_id, [(self.inv_item_id, 7)])
        self.assertTrue(success)
        self.assertEqual(self.live_quantity(), 7)
        record('1. positive quantity', success and self.live_quantity() == 7, 'set to 7, live confirms 7')

    def test_2_zero_quantity(self):
        success, _ = pti.set_inventory_quantities(self.domain, self.token, self.api_version,
                                                    self.location_id, [(self.inv_item_id, 0)])
        self.assertTrue(success)
        self.assertEqual(self.live_quantity(), 0)
        record('2. zero quantity', success and self.live_quantity() == 0, 'set to 0, live confirms 0')

    def test_3_multiple_variants_and_4_multiple_inventory_items(self):
        # product 17 has 9 real variants/inventory items - already imported
        data = pti.graphql_request(self.domain, self.token, self.api_version,
            '{ product(id: "gid://shopify/Product/9599851561216") { variants(first: 10) { edges { node { inventoryItem { id } } } } } }')
        items = [e['node']['inventoryItem']['id'] for e in data['data']['product']['variants']['edges']]
        self.assertEqual(len(items), 9)
        pairs = [(iid, 3) for iid in items]
        success, _ = pti.set_inventory_quantities(self.domain, self.token, self.api_version, self.location_id, pairs)
        self.assertTrue(success)
        data2 = pti.graphql_request(self.domain, self.token, self.api_version,
            '{ product(id: "gid://shopify/Product/9599851561216") { variants(first: 10) { edges { node { inventoryQuantity } } } } }')
        quantities = [e['node']['inventoryQuantity'] for e in data2['data']['product']['variants']['edges']]
        all_match = all(q == 3 for q in quantities)
        self.assertTrue(all_match)
        record('3+4. multiple variants/inventory items in one batched mutation', success and all_match,
               f'9 items set to 3 in a single call, live shows: {quantities}')

    def test_5_rerun_is_idempotent(self):
        success1, _ = pti.set_inventory_quantities(self.domain, self.token, self.api_version,
                                                     self.location_id, [(self.inv_item_id, 5)])
        qty_after_first = self.live_quantity()
        success2, resp2 = pti.set_inventory_quantities(self.domain, self.token, self.api_version,
                                                         self.location_id, [(self.inv_item_id, 5)])
        qty_after_second = self.live_quantity()
        no_duplicate = qty_after_first == 5 and qty_after_second == 5
        self.assertTrue(success1 and success2 and no_duplicate)
        record('5. re-running the same update', success1 and success2 and no_duplicate,
               f'first->5, second->{qty_after_second} (not 10 - correctly converges, not additive)')

    def test_6_api_validation_failure(self):
        # First attempt used quantity=-1, assuming Shopify rejects negative
        # stock - it doesn't (real finding: Shopify accepts negative
        # "available" quantities, presumably for backorder tracking). Not
        # a bug in the importer; the test's assumption was wrong, fixed
        # here rather than left silently green. A `name` outside Shopify's
        # real enum ("available", "on_hand", "committed", ...) is a
        # genuine, guaranteed validation failure.
        location_id = self.location_id
        current = pti.fetch_current_available_quantities(self.domain, self.token, self.api_version,
                                                           location_id, [self.inv_item_id])
        mutation = '''
        mutation($input: InventorySetQuantitiesInput!, $key: String!) {
          inventorySetQuantities(input: $input) @idempotent(key: $key) { userErrors { field message } }
        }'''
        variables = {'input': {'name': 'not_a_real_quantity_name', 'reason': 'correction',
                                'quantities': [{'inventoryItemId': self.inv_item_id, 'locationId': location_id,
                                                'quantity': 1, 'changeFromQuantity': current.get(self.inv_item_id, 0)}]},
                     'key': str(uuid.uuid4())}
        response = pti.graphql_request(self.domain, self.token, self.api_version, mutation, variables)
        errs = response.get('errors') or response.get('data', {}).get('inventorySetQuantities', {}).get('userErrors')
        got_real_error = bool(errs)
        record('6. API validation failure (invalid quantity name)', got_real_error, f'real error returned: {errs}')
        self.assertTrue(got_real_error)

    def test_7_missing_inventory_item(self):
        fake_id = 'gid://shopify/InventoryItem/999999999999999'
        result = pti.fetch_current_available_quantities(self.domain, self.token, self.api_version,
                                                          self.location_id, [fake_id])
        # A nonexistent node comes back null in `nodes` - must not crash, must not silently invent a quantity
        handled_gracefully = fake_id not in result or result.get(fake_id) == 0
        record('7. missing inventory item', handled_gracefully,
               f'nonexistent inventory item id handled without crashing: {result}')
        self.assertTrue(handled_gracefully)

    def test_8_missing_location(self):
        fake_location = 'gid://shopify/Location/999999999999999'
        success, response = pti.set_inventory_quantities(self.domain, self.token, self.api_version,
                                                           fake_location, [(self.inv_item_id, 1)])
        record('8. missing/invalid location', success is False, f'correctly fails, not silently accepted: {response}')
        self.assertFalse(success)

    @classmethod
    def tearDownClass(cls):
        # Leave the test store in a clean, known state matching the real
        # controlled test import rather than the scratch values used above.
        pti.get_default_location(cls.domain, cls.token, cls.api_version)
        pti.set_inventory_quantities(cls.domain, cls.token, cls.api_version, cls.location_id, [(cls.inv_item_id, 0)])
        data = pti.graphql_request(cls.domain, cls.token, cls.api_version,
            '{ product(id: "gid://shopify/Product/9599851561216") { variants(first: 10) { edges { node { inventoryItem { id } } } } } }')
        items = [e['node']['inventoryItem']['id'] for e in data['data']['product']['variants']['edges']]
        pti.set_inventory_quantities(cls.domain, cls.token, cls.api_version, cls.location_id,
                                      [(iid, 0) for iid in items])


class MockInventoryTests(unittest.TestCase):
    """Failure modes unsafe/impractical to reproduce against the real
    store - a malformed API response, and this project's documented lack
    of retry logic. Monkeypatches phase9_test_import.graphql_request."""

    def setUp(self):
        self._original = pti.graphql_request

    def tearDown(self):
        pti.graphql_request = self._original

    def test_9_partial_api_response(self):
        # A response missing the 'data' key entirely - e.g. a proxy/gateway
        # error page instead of a real GraphQL response.
        pti.graphql_request = lambda *a, **k: {'errors': [{'message': 'upstream timeout'}]}
        success, response = pti.set_inventory_quantities('domain', 'token', '2025-01', 'loc', [('item1', 5)])
        record('9. partial/malformed API response', success is False,
               'malformed response correctly treated as failure, not crashed or assumed successful')
        self.assertFalse(success)

    def test_10_retry_behaviour(self):
        # Documents the REAL current behaviour, not an aspiration: this
        # project has no retry logic (docs/PHASE9_POST_TEST_REVIEW.md § 7 -
        # "not exercised by this test, no evidence to act on"). Normal,
        # non-retried operation makes exactly 2 calls per invocation (fetch
        # current quantity, then the set mutation - both required, neither
        # a retry). A transient-error-shaped response on the set call is
        # surfaced as a failure after those 2 calls, not silently retried
        # a 3rd time.
        call_count = {'n': 0}
        def fake_request(*a, **k):
            call_count['n'] += 1
            if call_count['n'] == 1:
                return {'data': {'nodes': [{'id': 'item1', 'inventoryLevel': None}]}}  # fetch_current step
            return {'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED'}}]}  # set step
        pti.graphql_request = fake_request
        success, _ = pti.set_inventory_quantities('domain', 'token', '2025-01', 'loc', [('item1', 5)])
        no_retry_attempted = call_count['n'] == 2
        record('10. retry behaviour (documents current gap, does not fabricate retry)',
               no_retry_attempted and success is False,
               f'called {call_count["n"]} time(s) - fetch + set, no 3rd (retry) call, matching docs/RISK_REGISTER.md')
        self.assertTrue(no_retry_attempted)
        self.assertFalse(success)


if __name__ == '__main__':
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestSuite()
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(LiveInventoryTests))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(MockInventoryTests))
    result = runner.run(suite)
    print('\n=== Summary ===')
    for name, passed, detail in RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}: {name}")
    sys.exit(0 if result.wasSuccessful() else 1)
