import json, re, math, datetime, sys, os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import build_sellerboard
import build_extras
import build_shopify

R = '/tmp/refresh/'

def load(f):
    return json.load(open(R+f))

traffic30_raw = load('traffic_30d_daily.json')
traffic7_raw = load('traffic_7d.json')
traffic30 = traffic30_raw['data']   # daily
traffic7 = traffic7_raw['data']     # daily
trafficWeekly = load('traffic_weekly.json')['data']    # weekly buckets
ads30 = load('ads_30d.json')['data']
ads7 = load('ads_7d.json')['data']
adsType30 = load('ads_adtype.json')['data']
adsCampaign = load('ads_campaign_full.json')['data']
rankData = load('rank_p90.json')['data']
sqp = load('sqp.json')
pacing = load('pacing.json')['data'][0]

# ---- carry-forward from current live index.html ----
cur_html = open('/tmp/gh_repo/index.html').read()
m = re.search(r'<script id="metrics-data" type="application/json">(.*?)</script>', cur_html, re.S)
cur = json.loads(m.group(1))
cur_skus_by_asin = {s['asin']: s for s in cur['skus']}
inventory_carry = cur['inventory']
marginMeta_carry = cur['marginMeta']

AD_TYPE_LABELS = {"SP":"Sponsored Products","SB":"Sponsored Brands","SBV":"Sponsored Brands Video","SD":"Sponsored Display"}

def classify(name):
    n = name or ''
    if re.search(r'SDTA|cSDTA|SDPT', n):
        return 'display_targeting'
    if re.search(r'S[BPV]*KE', n) or re.search(r'GT SPKE', n):
        return 'exact'
    if re.search(r'S[BPV]*KP', n, re.I) or 'keyphrase' in n.lower():
        return 'phrase'
    if re.search(r'S[BPV]*KB', n):
        return 'broad'
    if re.search(r'SPTA|SPTC', n):
        return 'product_targeting'
    if re.search(r'SPA|SPAQL|SPAQC|SPAAS|SPAAC', n) or 'catch all - auto' in n.lower() or 'autos80' in n.lower():
        return 'auto'
    return 'unclassified'

MATCH_LABELS = {
    'exact':'Exact match','phrase':'Phrase match','broad':'Broad match','auto':'Auto targeting',
    'product_targeting':'Product targeting (ASIN/category)','display_targeting':'Display targeting',
    'unclassified':'Unclassified'
}

def derive(row):
    imp = row.get('impressions',0) or 0
    clk = row.get('clicks',0) or 0
    spend = row.get('spend',0) or 0
    sales = row.get('revenue', row.get('sales',0)) or 0
    units = row.get('units',0) or 0
    orders = row.get('purchases', row.get('orders',0)) or 0
    ctr = round(clk/imp,4) if imp else 0
    cvr = round(orders/clk,4) if clk else 0
    acos = round(spend/sales,4) if sales else None
    roas = round(sales/spend,2) if spend else 0
    return ctr,cvr,acos,roas

# ============ ACCOUNT + SKU 30D TOTALS (from daily traffic) ============
traffic30_by_asin = defaultdict(lambda: {'sessions':0,'units':0,'sales':0.0})
for r in traffic30:
    a = traffic30_by_asin[r['asin']]
    a['sessions'] += r['sessions']; a['units'] += r['units']; a['sales'] += r['sales']

traffic7_by_asin = defaultdict(lambda: {'sessions':0,'units':0,'sales':0.0})
for r in traffic7:
    a = traffic7_by_asin[r['asin']]
    a['sessions'] += r['sessions']; a['units'] += r['units']; a['sales'] += r['sales']

ads30_by_asin = {r['asin']: r for r in ads30}
ads7_by_asin = {r['asin']: r for r in ads7}

# ad-type by asin (30d)
adtype_by_asin = defaultdict(list)
for r in adsType30:
    adtype_by_asin[r['asin']].append(r)

# campaigns by asin (30d) with match-type classification
campaigns_by_asin = defaultdict(list)
for r in adsCampaign:
    campaigns_by_asin[r['asin']].append(r)

# weekly traffic by asin
weekly_by_asin = defaultdict(list)
for r in trafficWeekly:
    weekly_by_asin[r['asin']].append({'period': r['period'], 'sessions': r['sessions'], 'units': r['units'], 'sales': r['sales']})
for a in weekly_by_asin:
    weekly_by_asin[a].sort(key=lambda x: x['period'])

# BSR: build per-asin trend using leaf category preferred over generic 'health_and_beauty'
rank_by_asin = defaultdict(list)
for r in rankData:
    rank_by_asin[r['asin']].append(r)

def build_bsr(asin, category_hint):
    rows = rank_by_asin.get(asin, [])
    if not rows:
        return [], {}
    # pick leaf category name: prefer any categoryName != generic bucket
    leaf_names = set(r['categoryName'] for r in rows if r['categoryName'] != 'subcategory health_and_beauty')
    leaf = None
    if leaf_names:
        leaf = sorted(leaf_names)[0]
        use_rows = [r for r in rows if r['categoryName'] == leaf]
    else:
        leaf = 'subcategory health_and_beauty'
        use_rows = rows
    use_rows = sorted(use_rows, key=lambda r: r['date'])
    # weekly bucket - anchor Sunday-ending weeks
    buckets = {}
    for r in use_rows:
        d = datetime.date.fromisoformat(r['date'])
        days_to_sun = (6 - d.weekday()) % 7
        period = (d + datetime.timedelta(days=days_to_sun)).isoformat()
        buckets.setdefault(period, []).append(r['rank'])
    trend = [{'period': p, 'rank': round(sum(v)/len(v))} for p, v in sorted(buckets.items())]
    if not trend:
        return [], {}
    current = trend[-1]['rank']; currentDate = trend[-1]['period']
    earliest = trend[0]['rank']; earliestDate = trend[0]['period']
    idx30 = max(0, len(trend)-5)
    r30 = trend[idx30]['rank']; d30 = trend[idx30]['period']
    def pct(a,b):
        return round((b-a)/a*100,1) if a else None
    pctChange30d = pct(r30, current)
    pctChangeFull = pct(earliest, current)
    trendDir = 'down' if current < earliest else ('up' if current > earliest else 'flat')
    catLabel = leaf.replace('subcategory ', '').replace('_',' ').title() if leaf=='subcategory health_and_beauty' else leaf
    summary = {
        'category': category_hint or catLabel,
        'current': current, 'currentDate': currentDate,
        'rank30dAgo': r30, 'date30dAgo': d30,
        'rankEarliest': earliest, 'dateEarliest': earliestDate,
        'pctChange30d': pctChange30d, 'pctChangeFull': pctChangeFull,
        'trend': trendDir,
    }
    return trend, summary

all_asins = sorted(set(list(traffic30_by_asin.keys()) + list(cur_skus_by_asin.keys())))

skus = []
acct30 = {'sessions':0,'units':0,'sales':0.0,'impressions':0,'clicks':0,'spend':0.0,'revenue':0.0,'purchases':0,'adUnits':0}
acct7 = {'sessions':0,'units':0,'sales':0.0,'impressions':0,'clicks':0,'spend':0.0,'revenue':0.0,'purchases':0,'adUnits':0}

for asin in all_asins:
    cur_sku = cur_skus_by_asin.get(asin)
    if cur_sku is None:
        continue  # skip unknown asins not previously catalogued (avoid inventing SKU codes)
    t30 = traffic30_by_asin.get(asin, {'sessions':0,'units':0,'sales':0.0})
    t7 = traffic7_by_asin.get(asin, {'sessions':0,'units':0,'sales':0.0})
    a30 = ads30_by_asin.get(asin, {})
    a7 = ads7_by_asin.get(asin, {})

    imp30 = a30.get('impressions',0) or 0; clk30 = a30.get('clicks',0) or 0
    spend30 = a30.get('spend',0.0) or 0.0; rev30 = a30.get('revenue',0.0) or 0.0
    units30 = a30.get('units',0) or 0; ord30 = a30.get('purchases',0) or 0
    ctr30,cvr30,acos30,roas30 = derive(a30) if a30 else (0,0,None,0)
    tacos30 = round(spend30/t30['sales']*100,2) if t30['sales'] else 0

    imp7 = a7.get('impressions',0) or 0; clk7 = a7.get('clicks',0) or 0
    spend7 = a7.get('spend',0.0) or 0.0; rev7 = a7.get('revenue',0.0) or 0.0
    units7 = a7.get('units',0) or 0; ord7 = a7.get('purchases',0) or 0
    ctr7,cvr7,acos7,roas7 = derive(a7) if a7 else (0,0,None,0)
    tacos7 = round(spend7/t7['sales']*100,2) if t7['sales'] else 0

    # ad types
    ad_types_out = []
    for r in adtype_by_asin.get(asin, []):
        ctr,cvr,acos,roas = derive(r)
        ad_types_out.append({
            'adType': r['adType'], 'label': AD_TYPE_LABELS.get(r['adType'], r['adType']),
            'impressions': r['impressions'], 'clicks': r['clicks'], 'spend': r['spend'], 'sales': r['revenue'],
            'units': r['units'], 'orders': r['purchases'], 'ctr': ctr, 'cvr': cvr, 'acos': acos, 'roas': roas,
        })

    # campaigns + match types
    camp_rows = campaigns_by_asin.get(asin, [])
    campaigns_out = []
    match_agg = defaultdict(lambda: {'impressions':0,'clicks':0,'spend':0.0,'sales':0.0,'units':0,'orders':0})
    for r in camp_rows:
        mt = classify(r.get('campaignName',''))
        campaigns_out.append({
            'campaignName': r['campaignName'], 'theme': None, 'matchType': mt,
            'impressions': r['impressions'], 'clicks': r['clicks'], 'spend': r['spend'], 'sales': r['revenue'],
            'units': r['units'], 'orders': r['purchases'], 'ctr': r.get('ctr'), 'cvr': r.get('cr'),
            'acos': r.get('acos'), 'roas': r.get('roas'),
        })
        agg = match_agg[mt]
        agg['impressions'] += r['impressions']; agg['clicks'] += r['clicks']
        agg['spend'] += r['spend']; agg['sales'] += r['revenue']
        agg['units'] += r['units']; agg['orders'] += r['purchases']
    campaigns_out.sort(key=lambda c: -c['spend'])
    match_types_out = []
    for k, v in match_agg.items():
        ctr,cvr,acos,roas = derive({'impressions':v['impressions'],'clicks':v['clicks'],'spend':v['spend'],'revenue':v['sales'],'purchases':v['orders']})
        match_types_out.append({'key':k,'label':MATCH_LABELS.get(k,k),'impressions':v['impressions'],'clicks':v['clicks'],
            'spend':round(v['spend'],2),'sales':round(v['sales'],2),'ctr':ctr,'cvr':cvr,'acos':acos,'roas':roas})
    match_types_out.sort(key=lambda x: -x['spend'])

    bsrTrend, bsrSummary = build_bsr(asin, cur_sku.get('category'))
    if not bsrTrend:
        bsrTrend = cur_sku.get('bsrTrend', [])
        bsrSummary = cur_sku.get('bsrSummary', {})

    sku = {
        'asin': asin, 'sku': cur_sku['sku'], 'category': cur_sku.get('category'),
        'bsr': bsrSummary.get('current', cur_sku.get('bsr')),
        'sessions': t30['sessions'], 'totalUnits': t30['units'], 'totalSales': round(t30['sales'],2),
        'impressions': imp30, 'clicks': clk30, 'adSpend': round(spend30,2), 'adSales': round(rev30,2),
        'adUnits': units30, 'adOrders': ord30, 'ctr': ctr30, 'cvr': cvr30, 'acos': acos30, 'roas': roas30, 'tacos': tacos30,
        'weekly': weekly_by_asin.get(asin, cur_sku.get('weekly', [])),
        'campaigns': campaigns_out if campaigns_out else cur_sku.get('campaigns', []),
        'adTypes': ad_types_out if ad_types_out else cur_sku.get('adTypes', []),
        'matchTypes': match_types_out if match_types_out else cur_sku.get('matchTypes', []),
        'bsrTrend': bsrTrend, 'bsrSummary': bsrSummary,
        'metrics7d': {
            'sessions': t7['sessions'], 'totalUnits': t7['units'], 'totalSales': round(t7['sales'],2),
            'impressions': imp7, 'clicks': clk7, 'adSpend': round(spend7,2), 'adSales': round(rev7,2),
            'adUnits': units7, 'adOrders': ord7, 'ctr': ctr7, 'cvr': cvr7, 'acos': acos7, 'tacos': tacos7, 'roas': roas7,
        },
        'cogsPerUnit': cur_sku.get('cogsPerUnit'), 'cogsAsOf': cur_sku.get('cogsAsOf'),
    }
    skus.append(sku)

    acct30['sessions'] += t30['sessions']; acct30['units'] += t30['units']; acct30['sales'] += t30['sales']; acct30['adUnits'] += units30
    acct30['impressions'] += imp30; acct30['clicks'] += clk30; acct30['spend'] += spend30; acct30['revenue'] += rev30; acct30['purchases'] += ord30
    acct7['sessions'] += t7['sessions']; acct7['units'] += t7['units']; acct7['sales'] += t7['sales']; acct7['adUnits'] += units7
    acct7['impressions'] += imp7; acct7['clicks'] += clk7; acct7['spend'] += spend7; acct7['revenue'] += rev7; acct7['purchases'] += ord7

def acct_out(a):
    ctr = round(a['clicks']/a['impressions'],4) if a['impressions'] else 0
    cvr = round(a['purchases']/a['clicks'],4) if a['clicks'] else 0
    acos = round(a['spend']/a['revenue'],4) if a['revenue'] else None
    roas = round(a['revenue']/a['spend'],2) if a['spend'] else 0
    tacos = round(a['spend']/a['sales']*100,2) if a['sales'] else 0
    return {
        'totalSales': round(a['sales'],2), 'sessions': a['sessions'], 'totalUnits': a['units'],
        'impressions': a['impressions'], 'clicks': a['clicks'], 'adSpend': round(a['spend'],2), 'adSales': round(a['revenue'],2),
        'adUnits': a['adUnits'], 'adOrders': a['purchases'], 'ctr': ctr, 'cvr': cvr, 'acos': acos, 'roas': roas, 'tacos': tacos,
    }

account_out = acct_out(acct30)
account7d_out = acct_out(acct7)

# account-level ad type & match type rollups (30d)
acct_adtype_agg = defaultdict(lambda: {'impressions':0,'clicks':0,'spend':0.0,'sales':0.0,'orders':0})
for r in adsType30:
    agg = acct_adtype_agg[r['adType']]
    agg['impressions'] += r['impressions']; agg['clicks'] += r['clicks']; agg['spend'] += r['spend']; agg['sales'] += r['revenue']; agg['orders'] += r.get('purchases',0)
acctAdTypes = []
for k,v in acct_adtype_agg.items():
    ctr,cvr,acos,roas = derive({'impressions':v['impressions'],'clicks':v['clicks'],'spend':v['spend'],'revenue':v['sales'],'purchases':v['orders']})
    acos = round(v['spend']/v['sales'],4) if v['sales'] else None
    acctAdTypes.append({'adType':k,'label':AD_TYPE_LABELS.get(k,k),'impressions':v['impressions'],'clicks':v['clicks'],
        'spend':round(v['spend'],2),'sales':round(v['sales'],2),'ctr':ctr,'cvr':cvr,'acos':acos,'roas':roas})
acctAdTypes.sort(key=lambda x: -x['spend'])

acct_match_agg = defaultdict(lambda: {'impressions':0,'clicks':0,'spend':0.0,'sales':0.0,'orders':0})
for r in adsCampaign:
    mt = classify(r.get('campaignName',''))
    agg = acct_match_agg[mt]
    agg['impressions'] += r['impressions']; agg['clicks'] += r['clicks']; agg['spend'] += r['spend']; agg['sales'] += r['revenue']; agg['orders'] += r['purchases']
acctMatchTypes = []
for k,v in acct_match_agg.items():
    ctr,cvr,acos,roas = derive({'impressions':v['impressions'],'clicks':v['clicks'],'spend':v['spend'],'revenue':v['sales'],'purchases':v['orders']})
    acos = round(v['spend']/v['sales'],4) if v['sales'] else None
    acctMatchTypes.append({'key':k,'label':MATCH_LABELS.get(k,k),'impressions':v['impressions'],'clicks':v['clicks'],
        'spend':round(v['spend'],2),'sales':round(v['sales'],2),'ctr':ctr,'cvr':cvr,'acos':acos,'roas':roas})
acctMatchTypes.sort(key=lambda x: -x['spend'])

# bsrMeta (earliest/latest across pull)
all_dates = [r['date'] for r in rankData]
bsrMeta = {'earliestDate': min(all_dates), 'latestDate': max(all_dates)} if all_dates else cur.get('bsrMeta', {})

# pacing passthrough
pacing_out = {
    'available': pacing['available'], 'inbound': pacing['inbound'], 'velocityPerDay': round(pacing['velocityPerDay'],1),
    'runwayDays': round(pacing['runwayDays'],1), 'runwayWithInboundDays': round(pacing['runwayWithInboundDays'],1),
    'worstAsin': pacing['worstAsin'],
    'worstSku': cur_skus_by_asin.get(pacing['worstAsin'], {}).get('sku', pacing.get('worstAsinLabel')),
    'worstAsinRunway': pacing['worstAsinRunway'], 'action': pacing['action'], 'severity': pacing['severity'],
    'rationale': pacing['rationale'],
}

weekly_acct_agg = defaultdict(lambda: {'sales':0.0,'units':0,'sessions':0})
for r in trafficWeekly:
    agg = weekly_acct_agg[r['period']]
    agg['sales'] += r['sales']; agg['units'] += r['units']; agg['sessions'] += r['sessions']
weekly_acct = [{'period':p,'sales':round(v['sales'],2),'units':v['units'],'sessions':v['sessions']} for p,v in sorted(weekly_acct_agg.items())]

out = {
    'account': account_out,
    'skus': skus,
    'weekly': weekly_acct,
    'inventory': inventory_carry,
    'sqp': sqp,
    'meta': {'dateFirst': traffic30_raw['meta']['dateFirst'], 'dateLast': traffic30_raw['meta']['dateLast'], 'generated': datetime.date.today().isoformat()},
    'bsrMeta': bsrMeta,
    'acctAdTypes': acctAdTypes,
    'acctMatchTypes': acctMatchTypes,
    'account7d': account7d_out,
    'meta7d': {'dateFirst': traffic7_raw['meta']['dateFirst'], 'dateLast': traffic7_raw['meta']['dateLast']},
    'pacing': pacing_out,
    'marginMeta': marginMeta_carry,
}

# ---- Sellerboard (Profitability tab): net profit, margin, ROI, FBA stock, S&S ----
cur_inventory_by_sku = {r['sku']: r for r in inventory_carry.get('bySku', []) if r.get('sku')}
try:
    sb_raw_30d = json.load(open(R+'sb_raw_30d.json'))
    sb_raw_7d = json.load(open(R+'sb_raw_7d.json'))
    products30, totals30 = build_sellerboard.build(sb_raw_30d, cur_skus_by_asin, cur_inventory_by_sku)
    products7, totals7 = build_sellerboard.build(sb_raw_7d, cur_skus_by_asin, cur_inventory_by_sku)
    out['sellerboard'] = {
        'meta30': out['meta'], 'meta7': out['meta7d'],
        'products30': products30, 'products7': products7,
        'totals30': totals30, 'totals7': totals7,
    }
    # sanity check: aggregated total should be within 1% of the account-level total
    # Sellerboard itself reports (fetched separately via dashboard_period) -- if you
    # have that figure handy, compare it here before trusting the splice.
except FileNotFoundError as e:
    print("WARNING: Sellerboard raw pulls missing, carrying forward existing Profitability tab data:", e)
    out['sellerboard'] = cur.get('sellerboard')

# ---- WoW comparison, per-SKU impression share trend, monthly trend ----
try:
    tracked_asins = {s['asin'] for s in skus}
    extras = build_extras.build(out['account7d'], out['meta7d'], tracked_asins)
    out.update(extras)
except FileNotFoundError as e:
    print("WARNING: extras raw pulls missing, carrying forward existing wow/sqpBySku/monthlyTrend:", e)
    for k in ('wow', 'sqpBySku', 'monthlyTrend'):
        if k in cur:
            out[k] = cur[k]

# ---- Shopify (Omnichannel tab): retail / wholesale / Faire, account + per-SKU ----
try:
    out['shopify'] = build_shopify.build(out['meta'], out['meta7d'])
except FileNotFoundError as e:
    print("WARNING: Shopify raw pulls missing, carrying forward existing shopify data:", e)
    if 'shopify' in cur:
        out['shopify'] = cur['shopify']

json.dump(out, open(R+'metrics_out.json','w'))
print("Wrote metrics_out.json, skus:", len(skus))
print("account:", out['account'])
print("account7d:", out['account7d'])
print("sellerboard products30:", len(out.get('sellerboard', {}).get('products30', [])))
print("monthlyTrend months:", len(out.get('monthlyTrend', [])))
print("shopify channels30:", out.get('shopify', {}).get('channels30'))
print("shopify bySku30 count:", len(out.get('shopify', {}).get('bySku30', {})))
