"""
Build the `shopify` block: Amazon-adjacent sales data pulled from the Data
Brill Core Shopify connector for the Pure Micronutrients Shopify store (plus
its sibling Blyss Nutrition / Pure Dogs Co stores, which share some of the
same tracked SKUs).

Channels are derived from Shopify order tags, not a separate connector or
sales channel field:
  - 'Faire'      tag -> channel 'faire'      (orders placed via the Faire wholesale marketplace)
  - 'Wholesale'  tag (without 'Faire')  -> channel 'wholesale' (direct wholesale, not via Faire)
  - 'amazon'     tag -> excluded entirely (these are FBA/MCF orders synced INTO Shopify for
                          fulfillment -- already counted in the Amazon sales data; including
                          them here would double-count)
  - everything else  -> channel 'retail' (the normal DTC storefront)

Raw pull (see RUNBOOK.md step 2f): two SQL queries via `executeSql` per
window (30d, 7d), across all three connectors (Pure Micronutrients, Blyss
Nutrition, Pure Dogs Co) since a handful of tracked SKUs are cross-sold
between them:
  1. grouped by channel + shopify SKU + line-item title (sales/units) ->
     /tmp/refresh/shopify_channel_sku_30d.json / _7d.json
     each a flat list of {channel, shopify_sku, title, orders, units, sales}
     -- NOTE: the 'orders' field on these rows double-counts any order with
     multiple line items, so it is NOT used for the channel-level order count.
  2. grouped by channel only, counting DISTINCT order id ->
     /tmp/refresh/shopify_orders_by_channel_30d.json / _7d.json
     each a dict {"retail": N, "wholesale": N, "faire": N} -- this IS the
     accurate per-channel order count used below.

SHOPIFY_SKU_MAP below maps each observed Shopify variant SKU to the tracked
Amazon SKU code it represents (same product, different fulfillment channel --
FBA-fulfilled wholesale/Faire orders often literally reuse the Amazon SKU
code, while the regular retail storefront has its own Shopify-native variant
codes for the same products). This was built by manually cross-referencing
line-item titles against the 26 tracked SKUs' categories/ASINs -- it is
best-effort, not a guaranteed-exhaustive catalog match. Unmapped SKUs
(bundles, the "Shipping Protection" add-on, and any variant not yet seen)
fall into an 'other' bucket per channel so revenue is never silently dropped,
just not attributed to one SKU. Extend this map if a new Shopify variant
SKU shows up in the 'other' bucket for real product (not bundle/addon)
revenue.
"""

SHOPIFY_SKU_MAP = {
    # exact-code matches (Shopify variant SKU literally equals the Amazon SKU code)
    'D35-VK2-FBA1': 'D35-VK2-FBA1',
    'GC-L52G-XEJ6': 'GC-L52G-XEJ6',
    'MG-H6GM-QVRO': 'MG-H6GM-QVRO',
    'I2-8KDH-PURRES': 'I2-8KDH-PURRES',
    'VAG-PRO-FBA60': 'VAG-PRO-FBA60',
    'UT-SUPPORT-FBA': 'UT-SUPPORT-FBA',
    # title-matched (Shopify-native variant SKU, mapped by product identity)
    '5O-Z3G1-FRK8': 'SA-FFRON-FBA',
    '5O-Z3G1-ABCD': 'SA-FFRON-FBA',
    'ORG-SAFF-CAP45:1': 'SA-FFRON-FBA',
    'GN-DB5H-JZMH': 'MAG-CAPS-FBA180',
    'IP-4LJB-NYT3': 'ORG-CURC-FBA60',
    'SO-NA26-0BU9': 'IRON-PLUS-FBA1',
    'I2-8KDH-5KRZ-AMBER': 'ORG-RES-FBA90',
    '3J-8V5Q-MNA1': '5HTP-FBA60',
    'YA-86F4-Q4YI': 'FISH-OIL-FBA90',
    '39-2MLQ-H490': 'BONE-S-FBA1',
    'TJ-MTZ6-X8MF': 'TJ-MTZ6-FBA1',
    'KJ-IWJK-DKMT': 'BLYS-SEAMOSS-VI90',
    '4W-F2IU-SA9M': 'PROSTATE-FBA60',
    'UT-SUPPORT-FBA-AMBER': 'UT-SUPPORT-FBA',
    'MENO-SUP-WS60': 'MENO-SUP-FBA60',
    'OC-EU9P-1UYR': 'D3-5000-FBA1',
    'MAG-POWDER-HG': 'MAG-POWDER-FBA',
    '1W-2ABD-BX56': 'MEGA-ZINC-FBA100',
    # Blyss Nutrition / Pure Dogs Co connectors (cross-sold tracked SKUs)
    'PDC-CALMING': 'PDC-CALM-CHW90',
    'PDC-HIPJOINT': 'PDS-HIPJOINT-CHW90',
    'BLYSS-GOODVIBES': 'BLYS-SAFFRON-60',
    'BLYSS-UNWIND': 'BLYS-MAG-UW120',
    'BLYSS-SWEETDREAMS': 'BLYS-5HTP-60',
}

R = '/tmp/refresh/'


def _empty_channels():
    return {
        'retail': {'sales': 0.0, 'units': 0, 'orders': 0},
        'wholesale': {'sales': 0.0, 'units': 0, 'orders': 0},
        'faire': {'sales': 0.0, 'units': 0, 'orders': 0},
    }


def _process(rows, order_counts):
    """rows: list of {channel, shopify_sku, title, orders, units, sales}.
    order_counts: dict {channel: distinct_order_count} -- the accurate
    per-channel order count (rows[i]['orders'] is NOT used for this, since it
    double-counts orders that touch multiple SKUs).
    Returns (channels_totals, by_sku, other_untracked)."""
    channels = _empty_channels()
    by_sku = {}
    other = _empty_channels()

    for ch, n in (order_counts or {}).items():
        if ch in channels:
            channels[ch]['orders'] = n

    for r in rows:
        ch = r['channel']
        if ch == 'exclude_amazon_sync':
            continue
        if ch not in channels:
            continue
        channels[ch]['sales'] += r['sales']
        channels[ch]['units'] += r['units']

        tracked = SHOPIFY_SKU_MAP.get(r['shopify_sku'])
        if tracked:
            slot = by_sku.setdefault(tracked, _empty_channels())
            slot[ch]['sales'] += r['sales']
            slot[ch]['units'] += r['units']
        else:
            other[ch]['sales'] += r['sales']
            other[ch]['units'] += r['units']

    for d in channels.values():
        d['sales'] = round(d['sales'], 2)
    for d in other.values():
        d['sales'] = round(d['sales'], 2)
    for sku_d in by_sku.values():
        for d in sku_d.values():
            d['sales'] = round(d['sales'], 2)

    return channels, by_sku, other


def build(meta30, meta7):
    import json
    rows30 = json.load(open(R + 'shopify_channel_sku_30d.json'))
    rows7 = json.load(open(R + 'shopify_channel_sku_7d.json'))
    orders30 = json.load(open(R + 'shopify_orders_by_channel_30d.json'))
    orders7 = json.load(open(R + 'shopify_orders_by_channel_7d.json'))

    channels30, bySku30, other30 = _process(rows30, orders30)
    channels7, bySku7, other7 = _process(rows7, orders7)

    return {
        'meta30': meta30, 'meta7': meta7,
        'channels30': channels30, 'channels7': channels7,
        'bySku30': bySku30, 'bySku7': bySku7,
        'otherUntracked30': other30, 'otherUntracked7': other7,
    }
