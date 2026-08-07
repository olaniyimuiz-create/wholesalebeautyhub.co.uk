"""
CSV Generator Engine: reads migration/data/products.json and customers.json
(written by database_parser.py) and writes Shopify-importable CSVs to
shopify-theme/assets/.

Column layouts follow Shopify's documented product and customer CSV import
templates (help.shopify.com/en/manual/products/import-export/using-csv and
.../customers/import-export-customers).
"""
import csv
import json
import os

DATA_DIR = os.path.join('migration', 'data')
OUTPUT_DIR = os.path.join('shopify-theme', 'assets')

STORE_WEIGHT_UNIT = 'kg'  # must match database_parser.py's STORE_WEIGHT_UNIT

WEIGHT_TO_GRAMS = {'g': 1, 'kg': 1000, 'oz': 28.3495, 'lb': 453.592}

PRODUCT_HEADERS = [
    'Handle', 'Title', 'Body (HTML)', 'Vendor', 'Type', 'Tags', 'Published',
    'Option1 Name', 'Option1 Value', 'Option2 Name', 'Option2 Value',
    'Option3 Name', 'Option3 Value',
    'Variant SKU', 'Variant Grams', 'Variant Inventory Tracker',
    'Variant Inventory Qty', 'Variant Inventory Policy',
    'Variant Fulfillment Service', 'Variant Price', 'Variant Compare At Price',
    'Variant Requires Shipping', 'Variant Taxable', 'Variant Barcode',
    'Image Src', 'Image Position', 'Image Alt Text',
    'Gift Card', 'SEO Title', 'SEO Description',
    'Variant Image', 'Variant Weight Unit', 'Cost per item', 'Status',
]

CUSTOMER_HEADERS = [
    'First Name', 'Last Name', 'Email', 'Accepts Email Marketing',
    'Default Address Company', 'Default Address Address1',
    'Default Address Address2', 'Default Address City',
    'Default Address Province Code', 'Default Address Country Code',
    'Default Address Zip', 'Default Address Phone', 'Phone',
    'Accepts SMS Marketing', 'Note', 'Tax Exempt', 'Tags',
]


def money(value):
    if value in (None, ''):
        return ''
    try:
        return f'{float(value):.2f}'
    except (TypeError, ValueError):
        return ''


def grams(weight_value):
    if not weight_value:
        return ''
    try:
        base = float(weight_value)
    except (TypeError, ValueError):
        return ''
    return str(round(base * WEIGHT_TO_GRAMS.get(STORE_WEIGHT_UNIT, 1000)))


def inventory_fields(item):
    """item is a product or variation dict. Returns (tracker, qty, policy).
    Untracked WooCommerce stock (manage_stock != 'yes') maps to an empty
    tracker, which tells Shopify not to track inventory - i.e. always
    available, matching WooCommerce's behaviour in that case."""
    if item.get('manage_stock') != 'yes':
        return '', '', 'continue'
    try:
        qty = int(float(item.get('stock_quantity') or 0))
    except (TypeError, ValueError):
        qty = 0
    policy = 'continue' if item.get('backorders') in ('yes', 'notify') else 'deny'
    return 'shopify', qty, policy


def blank_product_row():
    return {h: '' for h in PRODUCT_HEADERS}


def product_to_rows(product):
    rows = []
    is_variable = product['wc_type'] == 'variable' and bool(product['variations'])
    variants = product['variations'] if is_variable else [None]

    images = list(product['images'])
    if is_variable:
        for v in product['variations']:
            img = v.get('image')
            if img and img not in images:
                images.append(img)

    published = 'TRUE' if product['status'] == 'publish' else 'FALSE'
    status = 'active' if product['status'] == 'publish' else 'draft'
    tags = ', '.join(dict.fromkeys(product['categories'] + product['tags']))
    option_names = (product['variation_option_names'] or [])[:3]

    for idx, variant in enumerate(variants):
        row = blank_product_row()
        row['Handle'] = product['handle']

        if idx == 0:
            row['Title'] = product['title']
            row['Body (HTML)'] = product['body_html']
            row['Vendor'] = product['vendor']
            row['Type'] = product['categories'][0] if product['categories'] else ''
            row['Tags'] = tags
            row['Published'] = published
            row['Status'] = status
            row['Gift Card'] = 'FALSE'
            if images:
                row['Image Src'] = images[0]
                row['Image Position'] = 1

        for opt_idx, name in enumerate(option_names):
            row[f'Option{opt_idx + 1} Name'] = name
        if variant:
            for opt_idx, value in enumerate(variant['options'][:3]):
                row[f'Option{opt_idx + 1} Value'] = value
            if variant.get('image') and variant['image'] != row.get('Image Src'):
                row['Variant Image'] = variant['image']

        source = variant or product
        price = (variant['price'] if variant else product['price']) or product['regular_price']
        regular = variant['regular_price'] if variant else product['regular_price']
        compare = regular if regular and price and str(regular) != str(price) else ''
        tracker, qty, policy = inventory_fields(source)

        row['Variant SKU'] = (variant['sku'] if variant else product['sku']) or ''
        row['Variant Grams'] = grams((variant['weight'] if variant else '') or product['weight'])
        row['Variant Inventory Tracker'] = tracker
        row['Variant Inventory Qty'] = qty
        row['Variant Inventory Policy'] = policy
        row['Variant Fulfillment Service'] = 'manual'
        row['Variant Price'] = money(price)
        row['Variant Compare At Price'] = money(compare)
        row['Variant Requires Shipping'] = 'FALSE' if product['virtual'] else 'TRUE'
        row['Variant Taxable'] = 'TRUE' if product['tax_status'] == 'taxable' else 'FALSE'
        row['Variant Weight Unit'] = STORE_WEIGHT_UNIT

        rows.append(row)

    for position, src in enumerate(images[1:], start=2):
        row = blank_product_row()
        row['Handle'] = product['handle']
        row['Image Src'] = src
        row['Image Position'] = position
        rows.append(row)

    return rows


def write_products_csv(products, path):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_HEADERS)
        writer.writeheader()
        for product in products:
            for row in product_to_rows(product):
                writer.writerow(row)


def customer_to_row(customer):
    return {
        'First Name': customer['first_name'],
        'Last Name': customer['last_name'],
        'Email': customer['email'],
        'Accepts Email Marketing': 'no',
        'Default Address Company': customer['company'],
        'Default Address Address1': customer['address1'],
        'Default Address Address2': customer['address2'],
        'Default Address City': customer['city'],
        'Default Address Province Code': customer['province'],
        'Default Address Country Code': customer['country_code'],
        'Default Address Zip': customer['zip'],
        'Default Address Phone': customer['phone'],
        'Phone': customer['phone'],
        'Accepts SMS Marketing': 'no',
        'Note': '',
        'Tax Exempt': 'no',
        'Tags': 'imported-from-woocommerce,' + ('registered' if customer['is_registered'] else 'guest'),
    }


def write_customers_csv(customers, path):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CUSTOMER_HEADERS)
        writer.writeheader()
        for customer in customers:
            writer.writerow(customer_to_row(customer))


def main():
    products_path = os.path.join(DATA_DIR, 'products.json')
    customers_path = os.path.join(DATA_DIR, 'customers.json')
    if not os.path.isfile(products_path) or not os.path.isfile(customers_path):
        raise SystemExit(f'Error: run database_parser.py first (missing files under {DATA_DIR})')

    with open(products_path, 'r', encoding='utf-8') as f:
        products = json.load(f)
    with open(customers_path, 'r', encoding='utf-8') as f:
        customers = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    products_csv = os.path.join(OUTPUT_DIR, 'shopify_products_import.csv')
    customers_csv = os.path.join(OUTPUT_DIR, 'shopify_customers_import.csv')

    write_products_csv(products, products_csv)
    write_customers_csv(customers, customers_csv)

    print(f'Wrote {sum(len(product_to_rows(p)) for p in products):,} rows to {products_csv}')
    print(f'Wrote {len(customers):,} rows to {customers_csv}')


if __name__ == '__main__':
    main()
