"""
Database Parser: reads the Adminer MySQL dump at migration/sql/dump.sql
(a WordPress + WooCommerce export) and writes two intermediate JSON files
that csv_generator.py turns into Shopify import CSVs:

    migration/data/products.json
    migration/data/customers.json

No database server is needed - the dump is streamed and parsed in place
(see sql_utils.py). Only the handful of tables and meta_keys relevant to
products and customers are kept in memory; a WooCommerce store's full
postmeta table can be 1M+ rows, so being selective matters.
"""
import json
import os

from sql_utils import iter_insert_rows, php_unserialize

SQL_DUMP_PATH = os.path.join('migration', 'sql', 'dump.sql')
DATA_DIR = os.path.join('migration', 'data')

# These come from the store's WooCommerce settings (Settings > Products) and
# aren't present in the small set of tables we parse - adjust if this store
# uses different units/currency.
STORE_CURRENCY = 'GBP'
STORE_WEIGHT_UNIT = 'kg'  # one of: g, kg, oz, lb

PRODUCT_META_KEYS = {
    '_sku', '_regular_price', '_sale_price', '_price', '_weight',
    '_length', '_width', '_height', '_thumbnail_id', '_product_image_gallery',
    '_product_attributes', '_tax_status', '_tax_class', '_stock',
    '_stock_status', '_manage_stock', '_backorders', '_virtual',
    '_downloadable', '_visibility', '_sold_individually', '_featured',
}

CUSTOMER_META_KEYS = {'wp_capabilities'}

TARGET_TABLES = {
    'wp_posts', 'wp_postmeta', 'wp_terms', 'wp_term_taxonomy',
    'wp_term_relationships', 'wp_wc_product_meta_lookup',
    'wp_woocommerce_attribute_taxonomies', 'wp_users', 'wp_usermeta',
    'wp_wc_customer_lookup', 'wp_wc_order_addresses',
}

STAFF_ROLES = {'administrator', 'shop_manager', 'editor'}


def is_product_meta_key(key):
    return key in PRODUCT_META_KEYS or key.startswith('attribute_')


def is_customer_meta_key(key):
    return key in CUSTOMER_META_KEYS or key.startswith('billing_') or key.startswith('shipping_')


def load_dump(path):
    posts = {}
    postmeta = {}
    terms = {}
    term_taxonomy = {}
    term_relationships = {}
    product_meta_lookup = {}
    attribute_taxonomies = {}
    users = {}
    usermeta = {}
    customer_lookup = []
    order_billing_by_email = {}

    row_count = 0
    for table, row in iter_insert_rows(path, TARGET_TABLES):
        row_count += 1
        if row_count % 500000 == 0:
            print(f'  ...scanned {row_count:,} rows')

        if table == 'wp_posts':
            if row['post_type'] not in ('product', 'product_variation', 'attachment'):
                continue
            pid = int(row['ID'])
            posts[pid] = {
                'ID': pid,
                'post_parent': int(row['post_parent']),
                'post_title': row['post_title'],
                'post_content': row['post_content'],
                'post_excerpt': row['post_excerpt'],
                'post_status': row['post_status'],
                'post_name': row['post_name'],
                'post_type': row['post_type'],
                'guid': row['guid'],
                'menu_order': int(row['menu_order']),
            }

        elif table == 'wp_postmeta':
            key = row['meta_key']
            if not is_product_meta_key(key):
                continue
            pid = int(row['post_id'])
            postmeta.setdefault(pid, {})[key] = row['meta_value']

        elif table == 'wp_terms':
            terms[int(row['term_id'])] = {'name': row['name'], 'slug': row['slug']}

        elif table == 'wp_term_taxonomy':
            term_taxonomy[int(row['term_taxonomy_id'])] = {
                'term_id': int(row['term_id']),
                'taxonomy': row['taxonomy'],
            }

        elif table == 'wp_term_relationships':
            oid = int(row['object_id'])
            term_relationships.setdefault(oid, []).append(int(row['term_taxonomy_id']))

        elif table == 'wp_wc_product_meta_lookup':
            product_meta_lookup[int(row['product_id'])] = row

        elif table == 'wp_woocommerce_attribute_taxonomies':
            attribute_taxonomies[row['attribute_name']] = row['attribute_label'] or row['attribute_name']

        elif table == 'wp_users':
            uid = int(row['ID'])
            users[uid] = {
                'user_email': row['user_email'],
                'user_registered': row['user_registered'],
                'display_name': row['display_name'],
            }

        elif table == 'wp_usermeta':
            key = row['meta_key']
            if not is_customer_meta_key(key):
                continue
            uid = int(row['user_id'])
            usermeta.setdefault(uid, {})[key] = row['meta_value']

        elif table == 'wp_wc_customer_lookup':
            customer_lookup.append(row)

        elif table == 'wp_wc_order_addresses':
            if row['address_type'] != 'billing' or not row['email']:
                continue
            # Later rows (later orders) overwrite earlier ones, so guests end
            # up with their most recently used billing address.
            order_billing_by_email[row['email'].strip().lower()] = row

    print(f'  scanned {row_count:,} rows total')
    return {
        'posts': posts,
        'postmeta': postmeta,
        'terms': terms,
        'term_taxonomy': term_taxonomy,
        'term_relationships': term_relationships,
        'product_meta_lookup': product_meta_lookup,
        'attribute_taxonomies': attribute_taxonomies,
        'users': users,
        'usermeta': usermeta,
        'customer_lookup': customer_lookup,
        'order_billing_by_email': order_billing_by_email,
    }


def attribute_label(taxonomy_key, attribute_taxonomies):
    name = taxonomy_key[3:] if taxonomy_key.startswith('pa_') else taxonomy_key
    return attribute_taxonomies.get(name) or name.replace('-', ' ').replace('_', ' ').strip().title()


def build_products(data):
    posts = data['posts']
    postmeta = data['postmeta']
    terms = data['terms']
    term_taxonomy = data['term_taxonomy']
    term_relationships = data['term_relationships']
    product_meta_lookup = data['product_meta_lookup']
    attribute_taxonomies = data['attribute_taxonomies']

    taxonomy_slug_to_name = {}
    for tt in term_taxonomy.values():
        term = terms.get(tt['term_id'])
        if term:
            taxonomy_slug_to_name[(tt['taxonomy'], term['slug'])] = term['name']

    def terms_for(post_id, taxonomy):
        names = []
        for ttid in term_relationships.get(post_id, []):
            tt = term_taxonomy.get(ttid)
            if tt and tt['taxonomy'] == taxonomy:
                term = terms.get(tt['term_id'])
                if term:
                    names.append(term['name'])
        return names

    variations_by_parent = {}
    for post in posts.values():
        if post['post_type'] == 'product_variation':
            variations_by_parent.setdefault(post['post_parent'], []).append(post)

    attachment_src = {
        pid: post['guid'] for pid, post in posts.items() if post['post_type'] == 'attachment'
    }

    def to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    products = []
    for pid, post in posts.items():
        if post['post_type'] != 'product' or post['post_status'] == 'trash':
            continue

        meta = postmeta.get(pid, {})
        lookup = product_meta_lookup.get(pid, {})

        categories = terms_for(pid, 'product_cat')
        tags = terms_for(pid, 'product_tag')
        brands = terms_for(pid, 'pwb-brand') or terms_for(pid, 'product-brands')
        type_terms = terms_for(pid, 'product_type')
        wc_type = type_terms[0] if type_terms else 'simple'

        images = []
        thumb_id = meta.get('_thumbnail_id')
        if thumb_id and thumb_id.isdigit() and int(thumb_id) in attachment_src:
            images.append(attachment_src[int(thumb_id)])
        for gid in (meta.get('_product_image_gallery') or '').split(','):
            gid = gid.strip()
            if gid.isdigit() and int(gid) in attachment_src:
                src = attachment_src[int(gid)]
                if src not in images:
                    images.append(src)

        parsed_attrs = php_unserialize(meta.get('_product_attributes')) or {}
        variation_option_names = [
            attribute_label(key, attribute_taxonomies)
            for key, info in parsed_attrs.items()
            if isinstance(info, dict) and str(info.get('is_variation')) in ('1', 'True', 'true')
        ]

        variations = []
        for v in sorted(variations_by_parent.get(pid, []), key=lambda p: (p['menu_order'], p['ID'])):
            if v['post_status'] == 'trash':
                continue
            vmeta = postmeta.get(v['ID'], {})
            options = []
            for key, val in vmeta.items():
                if not key.startswith('attribute_') or not val:
                    continue
                attr_key = key[len('attribute_'):]
                if attr_key.startswith('pa_'):
                    value = taxonomy_slug_to_name.get((attr_key, val), val.replace('-', ' ').title())
                else:
                    value = val
                options.append(value)

            vthumb = vmeta.get('_thumbnail_id')
            variations.append({
                'id': v['ID'],
                'sku': vmeta.get('_sku') or '',
                'regular_price': vmeta.get('_regular_price') or '',
                'price': vmeta.get('_price') or vmeta.get('_regular_price') or '',
                'stock_quantity': vmeta.get('_stock'),
                'manage_stock': vmeta.get('_manage_stock') or meta.get('_manage_stock') or 'no',
                'backorders': vmeta.get('_backorders') or meta.get('_backorders') or 'no',
                'weight': vmeta.get('_weight') or '',
                'options': options,
                'image': attachment_src.get(int(vthumb)) if vthumb and vthumb.isdigit() else None,
            })

        regular_price = meta.get('_regular_price') or ''
        sale_price = meta.get('_sale_price') or ''
        price = meta.get('_price') or regular_price

        products.append({
            'id': pid,
            'handle': post['post_name'],
            'title': post['post_title'],
            'body_html': post['post_content'],
            'status': post['post_status'],
            'sku': meta.get('_sku') or lookup.get('sku') or '',
            'regular_price': regular_price,
            'sale_price': sale_price,
            'price': price,
            'stock_quantity': meta.get('_stock') or lookup.get('stock_quantity'),
            'manage_stock': meta.get('_manage_stock') or 'no',
            'backorders': meta.get('_backorders') or 'no',
            'weight': meta.get('_weight') or '',
            'tax_status': meta.get('_tax_status') or lookup.get('tax_status') or 'taxable',
            'virtual': meta.get('_virtual') == 'yes',
            'wc_type': wc_type,
            'categories': categories,
            'tags': tags,
            'vendor': brands[0] if brands else '',
            'images': images,
            'variation_option_names': variation_option_names,
            'variations': variations,
        })

    return products


def build_customers(data):
    usermeta = data['usermeta']
    order_billing_by_email = data['order_billing_by_email']

    customers = []
    seen_emails = set()
    for row in data['customer_lookup']:
        email = (row.get('email') or '').strip().lower()
        if not email or email in seen_emails:
            continue

        user_id = int(row['user_id']) if row.get('user_id') else None
        um = usermeta.get(user_id, {}) if user_id else {}

        caps = php_unserialize(um.get('wp_capabilities')) or {}
        if isinstance(caps, dict) and caps and 'customer' not in caps and (STAFF_ROLES & set(caps)):
            continue  # staff/admin account, not a real storefront customer

        seen_emails.add(email)

        address1 = um.get('billing_address_1', '')
        address2 = um.get('billing_address_2', '')
        phone = um.get('billing_phone', '')
        company = um.get('billing_company', '')

        if not address1:
            oa = order_billing_by_email.get(email)
            if oa:
                address1 = oa.get('address_1') or address1
                address2 = address2 or oa.get('address_2') or ''
                phone = phone or oa.get('phone') or ''
                company = company or oa.get('company') or ''

        customers.append({
            'first_name': row.get('first_name') or um.get('billing_first_name') or '',
            'last_name': row.get('last_name') or um.get('billing_last_name') or '',
            'email': row['email'],
            'company': company,
            'address1': address1,
            'address2': address2,
            'city': row.get('city') or um.get('billing_city') or '',
            'province': row.get('state') or um.get('billing_state') or '',
            'country_code': row.get('country') or um.get('billing_country') or '',
            'zip': row.get('postcode') or um.get('billing_postcode') or '',
            'phone': phone,
            'is_registered': user_id is not None,
        })

    return customers


def main():
    if not os.path.isfile(SQL_DUMP_PATH):
        raise SystemExit(f'Error: {SQL_DUMP_PATH} not found')

    print(f'Reading {SQL_DUMP_PATH} ...')
    data = load_dump(SQL_DUMP_PATH)

    products = build_products(data)
    customers = build_customers(data)

    variation_count = sum(len(p['variations']) for p in products)
    print(f'Parsed {len(products):,} products ({variation_count:,} variations) '
          f'and {len(customers):,} customers.')

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'products.json'), 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA_DIR, 'customers.json'), 'w', encoding='utf-8') as f:
        json.dump(customers, f, ensure_ascii=False, indent=2)

    print(f'Wrote {DATA_DIR}/products.json and {DATA_DIR}/customers.json')


if __name__ == '__main__':
    main()
