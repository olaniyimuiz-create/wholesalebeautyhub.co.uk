"""
Phase 9.7 pricing-safety regression tests: missing price must NEVER become
'0.00'. All pure/local - no Shopify credentials or network calls needed,
since is_valid_variation/has_price/partition_by_price/flag_missing_price
are pure functions over in-memory product/variation dicts (same pattern
as test_phase9_inventory.py's MOCK tests).

Run: python migration/scripts/test_phase9_pricing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from phase9_dry_run import (
    has_price, partition_by_price, partition_variations, flag_missing_price,
)
from phase9_test_import import set_simple_variant, set_variable_variants


def variation(vid, option, regular_price='', price='', manage_stock='no', stock_quantity=None, sku=''):
    return {'id': vid, 'options': [option] if option else [], 'regular_price': regular_price,
            'price': price, 'sku': sku, 'manage_stock': manage_stock, 'stock_quantity': stock_quantity, 'weight': '', 'backorders': 'no', 'image': None}


def simple_product(pid=1, regular_price='', price='', title='Test', manage_stock='no', stock_quantity=None):
    return {'id': pid, 'title': title, 'handle': f'test-{pid}', 'wc_type': 'simple', 'status': 'publish',
            'vendor': 'Vendor', 'product_type': '', 'sku': '', 'regular_price': regular_price, 'price': price,
            'sale_price': '', 'categories': [], 'tags': [], 'images': [], 'body_html': '',
            'manage_stock': manage_stock, 'stock_quantity': stock_quantity, 'variations': [],
            'variation_option_names': []}


def variable_product(pid, variations, title='Variable Test'):
    p = simple_product(pid=pid, title=title)
    p['wc_type'] = 'variable'
    p['variations'] = variations
    p['variation_option_names'] = ['Shade']
    return p


results = []


def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))
    print(('PASS' if condition else 'FAIL'), '-', name, ('' if condition else f'({detail})'))


def test_1_valid_regular_price():
    v = variation(1, 'Red', regular_price='10.00', price='')
    check('TEST 1: variant with valid regular_price -> has_price True', has_price(v))


def test_2_sale_and_regular_price():
    v = variation(2, 'Blue', regular_price='20.00', price='15.00')
    check('TEST 2: valid sale + regular price -> has_price True', has_price(v))
    # existing approved compare-at logic: price=sale (15), compare_at=regular (20) when different
    from phase9_dry_run import to_number
    reg = to_number(v['regular_price'])
    price = to_number(v['price']) or reg
    compare = reg if reg and price and reg != price else None
    check('TEST 2: sale-price selected as price, regular as compare_at', price == 15.0 and compare == 20.0,
          f'price={price} compare={compare}')


def test_3_empty_price_both():
    v = variation(3, 'Green', regular_price='', price='')
    check('TEST 3: empty regular_price AND price -> has_price False (SKIP)', not has_price(v))


def test_4_partial_pricing():
    priced = [variation(100 + i, f'Priced{i}', regular_price='30') for i in range(7)]
    unpriced = [variation(200 + i, f'Unpriced{i}', regular_price='', price='') for i in range(4)]
    p = variable_product(69, priced + unpriced)
    option_valid, skipped_option = partition_variations(p['variations'])
    priced_out, unpriced_out = partition_by_price(option_valid)
    check('TEST 4: 7 priced + 4 unpriced -> 0 option-skipped', len(skipped_option) == 0, str(len(skipped_option)))
    check('TEST 4: 7 priced + 4 unpriced -> 7 priced variants', len(priced_out) == 7, str(len(priced_out)))
    check('TEST 4: 7 priced + 4 unpriced -> 4 price-skipped', len(unpriced_out) == 4, str(len(unpriced_out)))
    issues = flag_missing_price([p])
    partial = [i for i in issues if i['code'] == 'partial_missing_price']
    check('TEST 4: parent NOT quarantined (partial_missing_price only, not no_source_price)',
          len(partial) == 1 and not any(i['code'] == 'no_source_price' for i in issues), str(issues))


def test_5_zero_priced_variants():
    unpriced = [variation(300 + i, f'Shade{i}', regular_price='', price='') for i in range(5)]
    p = variable_product(999, unpriced)
    issues = flag_missing_price([p])
    quarantined = [i for i in issues if i['code'] == 'no_source_price']
    check('TEST 5: variable product with zero priced variants -> QUARANTINE (no_source_price)',
          len(quarantined) == 1, str(issues))


def test_6_simple_no_price():
    p = simple_product(pid=888, regular_price='', price='')
    issues = flag_missing_price([p])
    quarantined = [i for i in issues if i['code'] == 'no_source_price']
    check('TEST 6: simple product with no price -> QUARANTINE (no_source_price)',
          len(quarantined) == 1, str(issues))


def test_7_never_fabricate_zero():
    # set_simple_variant must refuse (raise), never silently send '0.00'.
    p = simple_product(pid=777, regular_price='', price='')
    try:
        set_simple_variant('shop.myshopify.com', 'tok', '2025-01', 'gid://x/Product/1', 'gid://x/ProductVariant/1', p)
        check('TEST 7: set_simple_variant refuses to fabricate price for unpriced product', False, 'did not raise')
    except RuntimeError as e:
        check('TEST 7: set_simple_variant refuses to fabricate price for unpriced product', 'fabricate' in str(e))

    # set_variable_variants must never include a priceless variation's
    # option value in what it sends to Shopify (checked structurally,
    # without a live call, by inspecting partition_by_price's output -
    # the exact input set_variable_variants builds `values`/`bulk_inputs`
    # from).
    priced = [variation(1, 'A', regular_price='10')]
    unpriced = [variation(2, 'B', regular_price='', price='')]
    option_valid, _ = partition_variations(priced + unpriced)
    priced_out, _ = partition_by_price(option_valid)
    check('TEST 7: unpriced variation never appears in the priced set sent to Shopify',
          all(v['id'] != 2 for v in priced_out), str(priced_out))


def test_8_stray_zero_price_not_treated_as_valid():
    # A live Shopify variant left over from the old bug (price=0.00) must
    # not be treated as matching valid source data during reconciliation:
    # expected_variants() must not expect that unpriced variation at all,
    # so any stray £0.00 variant shows up as unexpected/extra rather than
    # silently validated.
    from phase9_test_reconcile import expected_variants
    priced = [variation(1, 'A', regular_price='10')]
    unpriced = [variation(2, 'B', regular_price='', price='')]
    p = variable_product(69, priced + unpriced)
    exp = expected_variants(p)
    check('TEST 8: expected_variants() excludes the unpriced variation entirely',
          len(exp) == 1 and exp[0]['option'] == 'A', str(exp))


def main():
    test_1_valid_regular_price()
    test_2_sale_and_regular_price()
    test_3_empty_price_both()
    test_4_partial_pricing()
    test_5_zero_priced_variants()
    test_6_simple_no_price()
    test_7_never_fabricate_zero()
    test_8_stray_zero_price_not_treated_as_valid()

    failed = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} passed.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
