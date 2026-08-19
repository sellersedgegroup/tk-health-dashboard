"""
Aggregate raw Sellerboard dashboard_table product rows (which can have multiple
legacy/duplicate SKU codes per ASIN) into one row per ASIN, matching the
dashboard's existing SKU/category catalog and cross-referencing 3PL (TFL)
inventory from the currently-live index.html.

Input files (raw dashboard_table results, list of {"Info": {...}, ...} objects,
already de-paginated / concatenated), one pair per window:
  R + 'sb_raw_30d.json'  -> list of raw product rows, 30-day window
  R + 'sb_raw_7d.json'   -> list of raw product rows, 7-day window

Output: two lists (products30, products7) plus totals30/totals7, in the exact
shape the dashboard's Profitability tab expects. Call build(window_rows) for
each window and assemble the final `sellerboard` dict yourself, e.g.:

    from build_sellerboard import build
    products30, totals30 = build(raw_30d_rows, cur_skus_by_asin, cur_inventory_bySku)
    products7,  totals7  = build(raw_7d_rows,  cur_skus_by_asin, cur_inventory_bySku)
    sellerboard = {
        'meta30': {...}, 'meta7': {...},
        'products30': products30, 'products7': products7,
        'totals30': totals30, 'totals7': totals7,
    }
"""
from collections import defaultdict

# Sellerboard fields that are confirmed placeholder/unreliable (identical across
# every product row regardless of SKU) as of the Aug 2026 build. Re-verify this
# periodically (compare a few rows) before trusting them again.
UNRELIABLE_FIELDS = {'ShippingToPrep', 'ShippingToFBA'}


def _primary(rows):
    """Pick the SKU row with the most units as the 'primary' row for rate-like /
    identity fields (price, cogs, bsr, name, etc.)."""
    return max(rows, key=lambda r: r['Info'].get('Units', 0) or 0)


def _stock_locations(agg, tfl_match):
    locs = []
    if agg['Stock'] > 0 or agg['Reserved'] > 0:
        locs.append({'name': 'Amazon FBA (US)', 'qty': int(agg['Stock'])})
    if agg['FBAPrepStock'] > 0:
        locs.append({'name': 'FBA Prep Center', 'qty': int(agg['FBAPrepStock'])})
    if agg['StockAWD'] > 0 or agg['InboundAWD'] > 0:
        locs.append({'name': 'Amazon Warehousing & Distribution (AWD)', 'qty': int(agg['StockAWD'])})
    if tfl_match and tfl_match.get('matched') and (tfl_match.get('available') or 0) > 0:
        locs.append({'name': 'The Fulfillment Lab (3PL reserve, Tampa)', 'qty': int(tfl_match['available'])})
    return locs


def build(raw_rows, cur_skus_by_asin, cur_inventory_by_sku):
    """raw_rows: list of {"Info": {...}} dicts from Sellerboard dashboard_table.
    cur_skus_by_asin: {asin: sku_record} from the live dashboard's DATA.skus.
    cur_inventory_by_sku: {sku: tfl_record} from the live dashboard's DATA.inventory.bySku.
    Returns (products_list, totals_dict)."""
    by_asin = defaultdict(list)
    for r in raw_rows:
        info = r['Info']
        asin = info.get('ASIN')
        if not asin:
            continue
        by_asin[asin].append(r)

    products = []
    for asin, rows in by_asin.items():
        primary = _primary(rows)['Info']
        agg = defaultdict(float)
        for r in rows:
            info = r['Info']
            for k in ('Sales', 'Units', 'NetProfit', 'GrossProfit', 'ProductCosts',
                      'AmazonFees', 'Advertising', 'SNS_Units', 'SNS_Sales',
                      'SNS_Subscriptions', 'Stock', 'Reserved', 'FBAPrepStock',
                      'StockAWD', 'InboundAWD', 'StockAtCostPrice', 'SentToFBA', 'Ordered'):
                agg[k] += info.get(k, 0) or 0

        cur_sku = cur_skus_by_asin.get(asin, {})
        sku_codes = sorted({r['Info'].get('SKU') for r in rows if r['Info'].get('SKU')})
        primary_sku = cur_sku.get('sku') or primary.get('SKU')
        aliases = [s for s in sku_codes if s != primary.get('SKU')]

        tfl_match = cur_inventory_by_sku.get(primary_sku) if cur_inventory_by_sku else None

        sales = round(agg['Sales'], 2)
        net_profit = round(agg['NetProfit'], 2)
        product_costs = round(agg['ProductCosts'], 2)
        advertising = round(agg['Advertising'], 2)

        products.append({
            'asin': asin,
            'sku': primary_sku,
            'skuAliases': aliases,
            'name': primary.get('Name'),
            'category': cur_sku.get('category'),
            'sales': sales,
            'units': int(agg['Units']),
            'netProfit': net_profit,
            'grossProfit': round(agg['GrossProfit'], 2),
            'productCosts': product_costs,
            'amazonFees': round(agg['AmazonFees'], 2),
            'advertising': advertising,
            'margin': round(net_profit / sales * 100, 2) if sales else None,
            'roi': round(net_profit / abs(product_costs) * 100, 2) if product_costs else None,
            'realAcos': round(abs(advertising) / sales * 100, 2) if sales else None,
            'price': primary.get('Price'),
            'cogsPerUnit': primary.get('Cost'),
            'bsr': primary.get('BSR') or None,
            'bsrCategory': (primary.get('bsr_category') or '').strip() or None,
            'sessions': primary.get('sessions'),
            'snsUnits': int(agg['SNS_Units']),
            'snsSales': round(agg['SNS_Sales'], 2),
            'snsSubscriptions': int(agg['SNS_Subscriptions']),
            'fbaStock': int(agg['Stock']),
            'fbaReserved': int(agg['Reserved']),
            'fbaStockValue': round(agg['StockAtCostPrice'], 2),
            'daysOfStockLeft': primary.get('DaysOfStockLeft'),
            'daysToReorder': primary.get('DaysToReorder'),
            'runningOutOfStock': primary.get('RunningOutOfStock'),
            'sentToFba': int(agg['SentToFBA']),
            'onOrder': int(agg['Ordered']),
            'stockLocations': _stock_locations(agg, tfl_match),
            'locationCount': len(_stock_locations(agg, tfl_match)),
            'inTfl': bool(tfl_match and tfl_match.get('matched')),
        })

    totals = {
        'sales': round(sum(p['sales'] for p in products), 2),
        'netProfit': round(sum(p['netProfit'] for p in products), 2),
        'grossProfit': round(sum(p['grossProfit'] for p in products), 2),
        'productCosts': round(sum(p['productCosts'] for p in products), 2),
        'amazonFees': round(sum(p['amazonFees'] for p in products), 2),
        'advertising': round(sum(p['advertising'] for p in products), 2),
        'snsUnits': sum(p['snsUnits'] for p in products),
        'snsSales': round(sum(p['snsSales'] for p in products), 2),
        'units': sum(p['units'] for p in products),
    }
    totals['margin'] = round(totals['netProfit'] / totals['sales'] * 100, 2) if totals['sales'] else None
    totals['roi'] = round(totals['netProfit'] / abs(totals['productCosts']) * 100, 2) if totals['productCosts'] else None

    return products, totals
