"""
Phase 5 - SEO & URL Mapping: builds a WooCommerce -> Shopify URL inventory
and redirect matrix from the SQL dump, plus link-integrity reports.

Analysis only - this script does not create redirects or change any URLs.
It reuses sql_utils.py (the same streaming parser database_parser.py uses)
and migration/data/products.json (already parsed) rather than re-deriving
product data.

Old URL patterns below were read from wp_options (permalink_structure,
woocommerce_permalinks) and confirmed by fetching live pages - see
docs/SEO_STRATEGY.md for the evidence and the new-URL decisions (blog
handle, brand-as-collection, tag fallback) recorded as ADRs in
docs/DECISIONS.md.

    Products    old /shop/{slug}/                  new /products/{slug}
    Categories  old /product-category/{slug}/       new /collections/{slug}
    Brands      old /brand/{slug}/                  new /collections/{slug}
    Tags        old /product-tag/{slug}/            new /collections/all (see SEO_STRATEGY.md)
    Pages       old /{slug}/ or /{parent}/{slug}/   new /pages/{slug}
    Blog posts  old /{slug}/                        new /blogs/news/{slug}
    Blog index  old /wbh-beauty-blog/                new /blogs/news
    Shop index  old /shop/                          new /collections/all
    Homepage    old /                                new /

Canonical domain is the non-www apex (wholesalebeautyhub.co.uk); the live
site already redirects www -> apex.
"""
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from sql_utils import iter_insert_rows
from phase9_dry_run import CATEGORY_CLEANUP_MAP, normalize_name

DUMP_PATH = os.path.join('migration', 'sql', 'dump.sql')
PRODUCTS_JSON = os.path.join('migration', 'data', 'products.json')
COLLECTIONS_JSON = os.path.join('shopify', 'foundation', 'collections.json')
REPORTS_DIR = 'reports'


def load_category_handle_overrides():
    """GitHub issue #39: 7 stray WooCommerce categories are consolidated
    into a different, already-existing collection (docs/SHOPIFY_FOUNDATION.md
    § Collection architecture, mechanically applied in phase9_dry_run.py).
    Their /product-category/ redirect must land on that real destination
    handle, not the raw WordPress term slug - the raw slug will never
    become a real collection, so redirecting there would be a 301 to a
    404. Reuses CATEGORY_CLEANUP_MAP (single source of truth for the
    mapping) rather than re-deriving it."""
    with open(COLLECTIONS_JSON, encoding='utf-8') as f:
        collections_data = json.load(f)
    handle_by_name = {normalize_name(c['name']): c['handle']
                       for c in collections_data['category_collections'] + collections_data['manual_promo_collections']}
    overrides = {}
    for raw_normalized, target_name in CATEGORY_CLEANUP_MAP.items():
        handle = handle_by_name.get(normalize_name(target_name))
        if handle:
            overrides[raw_normalized] = handle
    return overrides

OLD_DOMAIN = 'https://wholesalebeautyhub.co.uk'
BLOG_HANDLE = 'news'  # Shopify default; revisit in Phase 7 if the theme wants a different handle

SHOP_ARCHIVE_ID = 7
BLOG_INDEX_ID = 19339
HOMEPAGE_ID = 629

LINK_RE = re.compile(
    r'href=["\'](?:https?://(?:www\.)?wholesalebeautyhub\.co\.uk)?(/[^"\'#?]*)',
    re.IGNORECASE,
)


def normalize_path(path):
    if not path:
        return '/'
    if not path.startswith('/'):
        path = '/' + path
    if not path.endswith('/') and '.' not in path.rsplit('/', 1)[-1]:
        path += '/'
    return path


def load_terms_and_relationships():
    terms, term_taxonomy = {}, {}
    relationships = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_terms', 'wp_term_taxonomy', 'wp_term_relationships'}):
        if table == 'wp_terms':
            terms[int(row['term_id'])] = row
        elif table == 'wp_term_taxonomy':
            term_taxonomy[int(row['term_taxonomy_id'])] = row
        elif table == 'wp_term_relationships':
            relationships.setdefault(int(row['object_id']), []).append(int(row['term_taxonomy_id']))
    return terms, term_taxonomy, relationships


def load_pages_and_posts():
    pages, posts, menu_items = {}, {}, {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_posts'}):
        if row['post_type'] == 'page':
            pages[int(row['ID'])] = row
        elif row['post_type'] == 'post':
            posts[int(row['ID'])] = row
        elif row['post_type'] == 'nav_menu_item' and row['post_status'] == 'publish':
            menu_items[int(row['ID'])] = row
    return pages, posts, menu_items


MENU_ITEM_META_KEYS = {'_menu_item_type', '_menu_item_object', '_menu_item_object_id', '_menu_item_url'}


def load_menu_item_meta():
    meta = {}
    for table, row in iter_insert_rows(DUMP_PATH, {'wp_postmeta'}):
        if row['meta_key'] not in MENU_ITEM_META_KEYS:
            continue
        meta.setdefault(int(row['post_id']), {})[row['meta_key']] = row['meta_value']
    return meta


def page_old_path(page):
    """Pages resolve to a flat /{slug}/ on this site. WordPress normally
    nests child pages under their parent's slug, and one page here
    (Refund and Returns Policy, post_parent -> 'Terms and Condition') has a
    non-zero post_parent that would suggest /wholesalebeautyhub-2/refund_returns/
    - but the live site serves it at the flat /refund_returns/ (verified by
    fetching both; only the flat one resolves). WooCommerce registers its
    own core pages (refund/returns, terms) by ID via options rather than by
    parent/child page nesting, which is almost certainly why the DB's
    post_parent isn't reflected in the actual permalink. Trust the observed
    behaviour over the theoretical one."""
    return f"/{page['post_name']}/"


# WooCommerce/plugin account & checkout endpoints: always reachable through
# the header cart/account UI on every page, not through wp_nav_menu or
# in-content links, so absence from both isn't a real orphan signal.
WOOCOMMERCE_UTILITY_SLUGS = {
    'woocommerce_cart', 'checkout', 'my-account', 'wishlist', 'reviews',
    'receipt', 'receipt-2', 'customer-dashboard', 'bya-buy-again',
}

# Verified by fetching the live homepage footer on 2026-08-07 (see
# docs/SEO_STRATEGY.md) - linked from the theme's hardcoded footer, which
# isn't stored as wp_nav_menu data and so is invisible to this script.
FOOTER_VERIFIED_SLUGS = {'about', 'privacy-policy', 'refund_returns', 'wholesalebeautyhub-2', 'shipping-policy'}


def build_inventory():
    with open(PRODUCTS_JSON, 'r', encoding='utf-8') as f:
        products = json.load(f)

    category_handle_overrides = load_category_handle_overrides()
    terms, term_taxonomy, relationships = load_terms_and_relationships()
    pages, posts, menu_items = load_pages_and_posts()

    entities = []  # each: dict(entity_type, id, title, status, old_path, new_path, content)
    # (_menu_item_object, _menu_item_object_id) -> old_path, so nav menu targets can be resolved
    old_path_by_object = {}

    for p in products:
        old_path = f"/shop/{p['handle']}/"
        old_path_by_object[('product', p['id'])] = old_path
        entities.append({
            'entity_type': 'product',
            'id': p['id'],
            'title': p['title'],
            'status': p['status'],
            'old_path': old_path,
            'new_path': f"/products/{p['handle']}",
            'content': p['body_html'],
        })

    taxonomy_new_prefix = {
        'product_cat': '/collections/',
        'pwb-brand': '/collections/',
    }
    taxonomy_old_prefix = {
        'product_cat': '/product-category/',
        'pwb-brand': '/brand/',
        'product_tag': '/product-tag/',
    }
    for tt in term_taxonomy.values():
        taxonomy = tt['taxonomy']
        if taxonomy not in taxonomy_old_prefix:
            continue
        term = terms.get(int(tt['term_id']))
        if not term:
            continue
        old_path = f"{taxonomy_old_prefix[taxonomy]}{term['slug']}/"
        override_handle = category_handle_overrides.get(normalize_name(term['name'])) if taxonomy == 'product_cat' else None
        if taxonomy == 'product_tag':
            new_path = '/collections/all'  # Shopify has no native tag-archive page; see SEO_STRATEGY.md
        elif override_handle:
            # issue #39 consolidation - see load_category_handle_overrides()
            new_path = f"{taxonomy_new_prefix[taxonomy]}{override_handle}"
        else:
            new_path = f"{taxonomy_new_prefix[taxonomy]}{term['slug']}"
        # menu items reference taxonomy terms by term_id, not term_taxonomy_id
        old_path_by_object[(taxonomy, int(tt['term_id']))] = old_path
        entities.append({
            'entity_type': 'brand' if taxonomy == 'pwb-brand' else taxonomy,
            'id': int(tt['term_taxonomy_id']),
            'title': term['name'],
            'status': 'publish',
            'old_path': old_path,
            'new_path': new_path,
            'content': '',
            'category_cleanup_override': bool(override_handle),
        })

    for pid, page in pages.items():
        old_path_by_object[('page', pid)] = page_old_path(page)
        if pid in (SHOP_ARCHIVE_ID, BLOG_INDEX_ID, HOMEPAGE_ID):
            continue
        entities.append({
            'entity_type': 'page',
            'id': pid,
            'title': page['post_title'],
            'status': page['post_status'],
            'old_path': old_path_by_object[('page', pid)],
            'new_path': f"/pages/{page['post_name']}",
            'content': page['post_content'],
        })

    for pid, post in posts.items():
        old_path = f"/{post['post_name']}/"
        old_path_by_object[('post', pid)] = old_path
        entities.append({
            'entity_type': 'blog_post',
            'id': pid,
            'title': post['post_title'],
            'status': post['post_status'],
            'old_path': old_path,
            'new_path': f"/blogs/{BLOG_HANDLE}/{post['post_name']}",
            'content': post['post_content'],
        })

    # Special fixed routes
    old_path_by_object[('page', HOMEPAGE_ID)] = '/'
    entities.append({
        'entity_type': 'homepage', 'id': HOMEPAGE_ID, 'title': 'Homepage',
        'status': 'publish', 'old_path': '/', 'new_path': '/', 'content': '',
    })
    entities.append({
        'entity_type': 'shop_archive', 'id': SHOP_ARCHIVE_ID, 'title': 'Shop',
        'status': 'publish', 'old_path': '/shop/', 'new_path': '/collections/all', 'content': '',
    })
    blog_index = pages.get(BLOG_INDEX_ID) or {}
    entities.append({
        'entity_type': 'blog_index', 'id': BLOG_INDEX_ID,
        'title': blog_index.get('post_title', 'Blog'),
        'status': 'publish',
        'old_path': f"/{blog_index.get('post_name', 'blog')}/",
        'new_path': f"/blogs/{BLOG_HANDLE}",
        'content': '',
    })

    menu_meta = load_menu_item_meta()
    menu_linked_paths = resolve_menu_linked_paths(menu_items, menu_meta, old_path_by_object)

    return entities, menu_linked_paths


def resolve_menu_linked_paths(menu_items, menu_meta, old_path_by_object):
    """Reconstruct which URLs are reachable from the site's nav menus
    (wp_posts post_type=nav_menu_item + its _menu_item_* postmeta), so the
    orphan report reflects real navigability instead of just in-content
    links, which most WooCommerce product/page copy simply doesn't have."""
    paths = set()
    for item_id in menu_items:
        meta = menu_meta.get(item_id, {})
        item_type = meta.get('_menu_item_type')
        if item_type == 'custom':
            url = meta.get('_menu_item_url') or ''
            paths.add(normalize_path(re.sub(r'^https?://(www\.)?wholesalebeautyhub\.co\.uk', '', url)))
            continue
        object_name = meta.get('_menu_item_object')
        object_id = meta.get('_menu_item_object_id')
        if not object_name or not object_id or not object_id.isdigit():
            continue
        old_path = old_path_by_object.get((object_name, int(object_id)))
        if old_path:
            paths.add(old_path)
    return paths


def published(entities):
    return [e for e in entities if e['status'] == 'publish']


def write_redirect_matrix(entities, path):
    rows = published(entities)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['entity_type', 'entity_id', 'title', 'old_url', 'new_path', 'redirect_type', 'notes'])
        for e in rows:
            notes = ''
            if e['new_path'] == '/collections/all' and e['entity_type'] == 'product_tag':
                notes = 'No Shopify tag-archive equivalent; falls back to all-products. Consider a matching collection.'
            elif e.get('category_cleanup_override'):
                notes = 'Redirects to the consolidated collection per the approved category cleanup (issue #39), not a same-named collection - that name was never carried forward.'
            w.writerow([
                e['entity_type'], e['id'], e['title'],
                OLD_DOMAIN + e['old_path'], e['new_path'], '301', notes,
            ])
    return len(rows)


def write_duplicate_report(entities, path):
    by_old_path = {}
    for e in published(entities):
        by_old_path.setdefault(e['old_path'], []).append(e)
    dupes = {k: v for k, v in by_old_path.items() if len(v) > 1}

    # '/collections/all' is an intentional many-to-one fallback (all tags +
    # the shop archive land there by design - see SEO_STRATEGY.md), not a
    # collision worth flagging.
    by_new_path = {}
    for e in published(entities):
        if e['new_path'] == '/collections/all':
            continue
        by_new_path.setdefault(e['new_path'], []).append(e)
    new_collisions = {k: v for k, v in by_new_path.items() if len(v) > 1}

    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['collision_type', 'path', 'entity_type', 'entity_id', 'title'])
        for old_path, group in dupes.items():
            for e in group:
                w.writerow(['duplicate_old_url', old_path, e['entity_type'], e['id'], e['title']])
        for new_path, group in new_collisions.items():
            for e in group:
                w.writerow(['colliding_new_url', new_path, e['entity_type'], e['id'], e['title']])
    return len(dupes), len(new_collisions)


def write_orphan_report(entities, menu_linked_paths, path):
    unpublished = [e for e in entities if e['status'] != 'publish']

    live = published(entities)
    linked_paths = set(menu_linked_paths)
    for e in live:
        for match in LINK_RE.finditer(e['content'] or ''):
            linked_paths.add(normalize_path(match.group(1)))

    always_reachable = {'/', '/shop/'}
    not_in_menu_or_content = [
        e for e in live
        if e['old_path'] not in linked_paths and e['old_path'] not in always_reachable
    ]

    def classify(e):
        slug = e['old_path'].strip('/')
        if e['entity_type'] == 'page' and slug in WOOCOMMERCE_UTILITY_SLUGS:
            return 'woocommerce_utility_page_reachable_via_header_ui'
        if e['entity_type'] == 'page' and slug in FOOTER_VERIFIED_SLUGS:
            return 'confirmed_reachable_via_theme_footer_not_wp_menu'
        if e['entity_type'] == 'product':
            return 'not_in_nav_menu_reached_via_category_browsing_normally'
        if e['entity_type'] == 'product_tag':
            return 'not_in_nav_menu_woocommerce_tags_rarely_are'
        return 'not_reachable_via_menu_or_content_link'

    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['reason', 'entity_type', 'entity_id', 'title', 'old_url', 'status'])
        for e in unpublished:
            w.writerow(['not_published', e['entity_type'], e['id'], e['title'],
                        OLD_DOMAIN + e['old_path'], e['status']])
        for e in not_in_menu_or_content:
            w.writerow([classify(e), e['entity_type'], e['id'], e['title'],
                        OLD_DOMAIN + e['old_path'], e['status']])

    needs_review = [
        e for e in not_in_menu_or_content
        if classify(e) == 'not_reachable_via_menu_or_content_link'
    ]
    return len(unpublished), len(not_in_menu_or_content), len(needs_review)


def write_broken_link_report(entities, path):
    known_paths = {e['old_path'] for e in published(entities)}
    known_paths.update({'/shop/', '/'})
    broken = []
    for e in published(entities):
        for match in LINK_RE.finditer(e['content'] or ''):
            target = normalize_path(match.group(1))
            if target.startswith(('/wp-content/', '/wp-admin/', '/wp-json/', '/cart/', '/checkout/',
                                   '/my-account/', '/feed/')):
                continue  # WordPress/WooCommerce system routes, not content to migrate
            if target not in known_paths:
                broken.append((e, target))

    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source_entity_type', 'source_entity_id', 'source_title', 'source_url', 'broken_target'])
        for e, target in broken:
            w.writerow([e['entity_type'], e['id'], e['title'], OLD_DOMAIN + e['old_path'], OLD_DOMAIN + target])
    return len(broken)


def main():
    if not os.path.isfile(DUMP_PATH):
        raise SystemExit(f'Error: {DUMP_PATH} not found')
    if not os.path.isfile(PRODUCTS_JSON):
        raise SystemExit(f'Error: {PRODUCTS_JSON} not found - run database_parser.py first')

    print('Building URL inventory from dump.sql + products.json ...')
    entities, menu_linked_paths = build_inventory()
    print(f'Resolved {len(menu_linked_paths)} distinct paths from the site nav menus.')
    os.makedirs(REPORTS_DIR, exist_ok=True)

    redirect_count = write_redirect_matrix(entities, os.path.join(REPORTS_DIR, 'redirect_matrix.csv'))
    dup_old, dup_new = write_duplicate_report(entities, os.path.join(REPORTS_DIR, 'duplicate_urls.csv'))
    unpublished, not_in_menu, needs_review = write_orphan_report(
        entities, menu_linked_paths, os.path.join(REPORTS_DIR, 'orphan_urls.csv'))
    broken = write_broken_link_report(entities, os.path.join(REPORTS_DIR, 'broken_links.csv'))

    by_type = {}
    for e in published(entities):
        by_type[e['entity_type']] = by_type.get(e['entity_type'], 0) + 1

    print(f'Inventory by type: {by_type}')
    print(f'Redirect matrix: {redirect_count} rows -> reports/redirect_matrix.csv')
    print(f'Duplicate old URLs: {dup_old}, colliding new URLs: {dup_new} -> reports/duplicate_urls.csv')
    print(f'Unpublished (not redirected): {unpublished}')
    print(f'Not found in nav menu or content links: {not_in_menu} '
          f'(mostly expected - products/tags/utility pages; see reports/orphan_urls.csv)')
    print(f'  of which genuinely worth a human look: {needs_review}')
    print(f'Broken internal links found: {broken} -> reports/broken_links.csv')


if __name__ == '__main__':
    main()
