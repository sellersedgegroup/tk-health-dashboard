"""
Build the three "extras" blocks added on top of the core Data Brill Core refresh:
  - wow: this-week vs last-week ad/traffic comparison
  - sqpBySku: per-ASIN weekly impression-share trend (last ~6 weeks)
  - monthlyTrend: account-wide monthly sales/ads history (MoM now, YoY once
    12+ months of history exist)

Expects these raw pull files in R (see RUNBOOK.md step 2b for the exact calls):
  prior_week_traffic.json  -> loadTraffic result for the 7 days before the
                               current 7d window (groupBy=asin, timeUnit=WEEK)
  prior_week_ads.json      -> loadAds result for the same window
                               (groupBy=store, derived=true)
  sqp_by_sku_raw.json      -> {asin: loadSqp_result} for every tracked ASIN,
                               each called with products=<asin>, keywordLimit=1,
                               timeUnit=WEEK, when=P90D/<lastDate>
  traffic_monthly_raw.json -> loadTraffic result, groupBy=asin, timeUnit=MONTH,
                               when=P24M/<lastDate> (data may only go back to
                               whenever this account's history starts)
  ads_monthly_raw.json     -> loadAds result, groupBy=store, timeUnit=MONTH,
                               when=P24M/<lastDate>, derived=true
"""
import json
from collections import defaultdict

R = '/tmp/refresh/'


def load(f):
    return json.load(open(R + f))


def build_wow(account7d, meta7d):
    pw_traffic = load('prior_week_traffic.json')['data']
    pw_ads = load('prior_week_ads.json')['data'][0]

    sessions = sum(r['sessions'] for r in pw_traffic)
    units = sum(r['units'] for r in pw_traffic)
    sales = round(sum(r['sales'] for r in pw_traffic), 2)
    tacos = round(pw_ads['spend'] / sales * 100, 2) if sales else 0

    prior_week = {
        'dateFirst': pw_traffic[0].get('periodStart') or None,
        'dateLast': pw_traffic[0].get('period'),
        'totalSales': sales, 'sessions': sessions, 'totalUnits': units,
        'impressions': pw_ads['impressions'], 'clicks': pw_ads['clicks'],
        'adSpend': pw_ads['spend'], 'adSales': pw_ads['revenue'],
        'adUnits': pw_ads['units'], 'adOrders': pw_ads['purchases'],
        'ctr': pw_ads['ctr'], 'cvr': pw_ads['cr'], 'acos': pw_ads['acos'], 'roas': pw_ads['roas'],
        'tacos': tacos,
    }
    current_week = dict(account7d)
    current_week['dateFirst'] = meta7d['dateFirst']
    current_week['dateLast'] = meta7d['dateLast']
    return {'currentWeek': current_week, 'priorWeek': prior_week}


def build_sqp_by_sku(weeks_to_keep=6):
    raw = load('sqp_by_sku_raw.json')  # {asin: loadSqp_result}
    out = {}
    for asin, result in raw.items():
        periods = result.get('periods', [])
        if not periods:
            continue
        tail = periods[-weeks_to_keep:]
        out[asin] = {
            'periods': [p['period'] for p in tail],
            'imprShare': [p['imprShare'] for p in tail],
        }
    return out


def build_monthly_trend(tracked_asins):
    traffic = load('traffic_monthly_raw.json')['data']
    ads = load('ads_monthly_raw.json')['data']

    by_month = defaultdict(lambda: {'sessions': 0, 'units': 0, 'sales': 0.0})
    for r in traffic:
        if r['asin'] not in tracked_asins:
            continue
        m = by_month[r['period']]
        m['sessions'] += r['sessions']
        m['units'] += r['units']
        m['sales'] += r['sales']

    ads_by_month = {a['dateFirst'][:7]: a for a in ads}
    current_ym = max(by_month.keys())[:7] if by_month else None

    monthly = []
    for period in sorted(by_month.keys()):
        ym = period[:7]
        t = by_month[period]
        a = ads_by_month.get(ym, {})
        monthly.append({
            'month': ym,
            'sessions': t['sessions'], 'units': t['units'], 'sales': round(t['sales'], 2),
            'adSpend': a.get('spend'), 'adSales': a.get('revenue'),
            'impressions': a.get('impressions'), 'clicks': a.get('clicks'), 'acos': a.get('acos'),
            'partial': ym == current_ym,
        })
    return monthly


def build(account7d, meta7d, tracked_asins):
    return {
        'wow': build_wow(account7d, meta7d),
        'sqpBySku': build_sqp_by_sku(),
        'monthlyTrend': build_monthly_trend(tracked_asins),
    }
