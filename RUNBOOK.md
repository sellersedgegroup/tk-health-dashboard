# TK Health Dashboard — automated refresh runbook

This repo's `index.html` is refreshed on a schedule by a Claude session with
access to the `DataBrill_Core` MCP tools, the `SellerBoard` MCP tools, and a
shell. Each run is a **fresh session with no memory of prior runs** — follow
these steps exactly.

## 0. Setup

```
rm -rf /tmp/gh_repo /tmp/refresh
mkdir -p /tmp/refresh
git clone https://x-access-token:<GITHUB_PAT>@github.com/sellersedgegroup/tk-health-dashboard.git /tmp/gh_repo
```

(The PAT is provided in the scheduled task prompt — never write it to any
file that gets committed. It's fine to embed transiently in the git remote
URL / local `.git/config`, same as an interactive push.)

## 1. Compute the date windows

Data lags 1–2 days, so anchor on `lastDate = today - 2 days`.

```python
import datetime
today = datetime.date.today()
lastDate = today - datetime.timedelta(days=2)
d30_start = lastDate - datetime.timedelta(days=29)
d7_start  = lastDate - datetime.timedelta(days=6)
wk_start  = lastDate - datetime.timedelta(days=34)   # 5 weekly buckets
prevWeekEnd   = d7_start - datetime.timedelta(days=1)
prevWeekStart = prevWeekEnd - datetime.timedelta(days=6)
```

Get the list of currently-tracked SKUs/ASINs from the just-cloned repo (needed
for steps 2b and 2c below):

```python
import re, json
cur_html = open('/tmp/gh_repo/index.html').read()
m = re.search(r'<script id="metrics-data" type="application/json">(.*?)</script>', cur_html, re.S)
cur = json.loads(m.group(1))
tracked = [(s['sku'], s['asin']) for s in cur['skus']]  # ~26 entries
```

## 2a. Core Data Brill Core pull (unchanged from earlier versions of this runbook)

Call these `DataBrill_Core` tools (batch independent ones together):

1. `loadTraffic(stores=US, when="{d30_start}/{lastDate}", groupBy=asin, timeUnit=DAY)`
2. `loadTraffic(stores=US, when="{d7_start}/{lastDate}", groupBy=asin, timeUnit=DAY)`
3. `loadTraffic(stores=US, when="{wk_start}/{lastDate}", groupBy=asin, timeUnit=WEEK)`
4. `loadAds(stores=US, when="{d30_start}/{lastDate}", groupBy=asin, derived=true)`
5. `loadAds(stores=US, when="{d7_start}/{lastDate}", groupBy=asin, derived=true)`
6. `loadAds(stores=US, when="{d30_start}/{lastDate}", groupBy=asin,adType, derived=true)`
7. `loadAds(stores=US, when="{d30_start}/{lastDate}", groupBy=asin,campaign, derived=true)` — campaign-level, needed for match-type classification
8. `loadRank(stores=US, when="P90D/{lastDate}")`
9. `loadSqp(stores=US, when="{wk_start}/{lastDate}", timeUnit=WEEK)`
10. `inventoryPacing(stores=US)`

Save each result to `/tmp/refresh/` under these EXACT filenames:

- `traffic_30d_daily.json` ← call #1 (full tool result JSON incl. `meta`/`data`)
- `traffic_7d.json` ← call #2
- `traffic_weekly.json` ← call #3
- `ads_30d.json` ← call #4
- `ads_7d.json` ← call #5
- `ads_adtype.json` ← call #6
- `ads_campaign_full.json` ← call #7
- `rank_p90.json` ← call #8
- `sqp.json` ← call #9 (the tool's top-level `{periods, keywords}` object, NOT wrapped further)
- `pacing.json` ← call #10, wrapped as `{"data": [<the single family row>]}`

Some of these (30d-daily traffic, full-campaign ads, P90D rank) are large and
may come back as a saved tool-result file instead of inline — that's fine,
just `cp` that file to the target filename (no need to re-read it into context).

## 2b. Sellerboard pull (Profitability tab: net profit, margin, ROI, FBA stock, S&S)

1. `mcp__SellerBoard__dashboard_period(when="{d30_start}/{lastDate}" or equivalent 30d window)` —
   call once to "warm" the 30-day window. If it returns `status:"preparing"`,
   `sleep 8` and call again with **identical** arguments until `status:"success"`.
2. `mcp__SellerBoard__dashboard_table(entry_type="product", ...same 30d window..., page=1)`,
   then `page=2`, etc. until you've paged through all rows (`total` field tells you how many).
   Concatenate every page's `result` array into one list.
3. Repeat steps 1–2 for the 7-day window (`d7_start`/`lastDate`).

Save the concatenated raw row lists (list of `{"Info": {...}, ...}` objects,
NOT wrapped in anything else) to:
- `/tmp/refresh/sb_raw_30d.json`
- `/tmp/refresh/sb_raw_7d.json`

Rate limit: 5 requests burst then ~3/sec per account — space out the paged calls if needed.

**Do not trust `ShippingToPrep`/`ShippingToFBA`** — these have been observed
identical across every product row (placeholder data). `scripts/build_sellerboard.py`
already excludes them; don't reintroduce them without re-verifying first.

## 2c. Per-SKU search-impression-share trend (SQP)

For **every** `(sku, asin)` pair in `tracked` (from step 1, ~26 calls):

```
mcp__DataBrill_Core__loadSqp(stores=US, when="P90D/{lastDate}", timeUnit=WEEK, products="{asin}", keywordLimit=1)
```

Build `/tmp/refresh/sqp_by_sku_raw.json` as `{asin: <full loadSqp result>}` —
i.e. a dict keyed by ASIN, each value the raw tool result (which has a `periods`
array of `{period, imprShare, ...}` objects). `build_extras.py` will trim this
to the most recent 6 weeks itself — no need to pre-trim.

## 2d. This-week-vs-last-week ad comparison (WoW)

1. `loadTraffic(stores=US, when="{prevWeekStart}/{prevWeekEnd}", groupBy=asin, timeUnit=WEEK)` → save full result to `/tmp/refresh/prior_week_traffic.json`
2. `loadAds(stores=US, when="{prevWeekStart}/{prevWeekEnd}", groupBy=store, derived=true)` → save full result to `/tmp/refresh/prior_week_ads.json`

(The "current week" side is already covered by the `account7d` block the core
pull in 2a produces — no extra call needed for that half of the comparison.)

## 2e. Monthly trend (feeds the "Monthly Trend" tab; becomes true YoY once 12+ months of history exist)

1. `loadTraffic(stores=US, when="P24M/{lastDate}", groupBy=asin, timeUnit=MONTH)` → save to `/tmp/refresh/traffic_monthly_raw.json`
2. `loadAds(stores=US, when="P24M/{lastDate}", groupBy=store, timeUnit=MONTH, derived=true)` → save to `/tmp/refresh/ads_monthly_raw.json`

As of this runbook's last update, this account's history only goes back to
January 2026, so these calls return fewer months than requested — that's
expected, not an error. `build_extras.py` handles however many months come
back and will start computing real YoY deltas automatically once a given
month has a matching month 12 back in the data.

## 2f. Shopify (Omnichannel tab): retail / wholesale / Faire, account + per-SKU

Pure Micronutrients sells through Shopify as well as Amazon, split into three
channels identified by **Shopify order tags** (not a separate connector or
sales-channel field): orders tagged `Faire` are the Faire wholesale
marketplace, orders tagged `Wholesale` (without `Faire`) are direct wholesale,
everything else on the storefront is `retail`. Orders tagged `amazon` are
FBA/MCF orders that sync into Shopify for fulfillment — **always excluded**,
since they're already counted in the Amazon data pulled in step 2a; including
them here would double-count.

There are three Shopify connectors in this Data Brill Core workspace that all
feed tracked SKUs (a handful of SKUs — the `BLYS-*` and `PDC/PDS-*` ones — are
cross-sold through the sibling Blyss Nutrition / Pure Dogs Co stores, not just
the main Pure Micronutrients store): `01a01613-8a8c-773f-9b5f-50d601eb1bbf`
(Pure Micronutrients), `01a0163a-4ec3-71ea-9eee-101cabc3147b` (Blyss
Nutrition), `01a0163b-11b8-7691-be57-f3fd43fef128` (Pure Dogs Co). Always
query all three together.

Use `mcp__DataBrill_Core__executeSql` (read-only SQL) — align the date bounds
to the **same** `dateFirst`/`dateLast` already used for the Amazon 30d/7d
pulls in step 2a (`d30_start`/`lastDate` and `d7_start`/`lastDate`), so the
Omnichannel tab's Amazon and Shopify figures cover the same window. Use
`>= '{start}T00:00:00Z' AND < '{end+1 day}T00:00:00Z'` (i.e. the day after
`lastDate`, exclusive) for each window.

1. Channel + SKU + title breakdown (one call per window — 30d, then 7d):

```sql
SELECT
  CASE
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='Faire') THEN 'faire'
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='Wholesale') THEN 'wholesale'
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='amazon') THEN 'exclude_amazon_sync'
    ELSE 'retail'
  END AS channel,
  li.sku AS shopify_sku, li.title AS title,
  count(DISTINCT o.id) AS orders, sum(li.quantity) AS units, sum(li."discountedTotalAmount") AS sales
FROM "shopify_orders_v1__OrderLineItem" li
JOIN "shopify_orders_v1__Order" o ON o.id = li."orderGid"
WHERE o."connectorId" IN ('01a01613-8a8c-773f-9b5f-50d601eb1bbf','01a0163a-4ec3-71ea-9eee-101cabc3147b','01a0163b-11b8-7691-be57-f3fd43fef128')
  AND o.test = false
  AND o."shopifyCreatedAt" >= '{start}T00:00:00Z' AND o."shopifyCreatedAt" < '{end_exclusive}T00:00:00Z'
GROUP BY 1,2,3
```

Save as `/tmp/refresh/shopify_channel_sku_30d.json` / `_7d.json` — a flat
JSON list of the returned rows (`channel`, `shopify_sku`, `title`, `orders`,
`units`, `sales`). **Ignore the `orders` field on these rows when building
channel totals** — it double-counts any order with more than one line item.

2. Accurate per-channel order counts (one call per window):

```sql
SELECT
  CASE
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='Faire') THEN 'faire'
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='Wholesale') THEN 'wholesale'
    WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(o.doc->'tags') t WHERE t='amazon') THEN 'exclude_amazon_sync'
    ELSE 'retail'
  END AS channel,
  count(*) AS distinct_orders
FROM "shopify_orders_v1__Order" o
WHERE o."connectorId" IN ('01a01613-8a8c-773f-9b5f-50d601eb1bbf','01a0163a-4ec3-71ea-9eee-101cabc3147b','01a0163b-11b8-7691-be57-f3fd43fef128')
  AND o.test = false
  AND o."shopifyCreatedAt" >= '{start}T00:00:00Z' AND o."shopifyCreatedAt" < '{end_exclusive}T00:00:00Z'
GROUP BY 1
```

Save as `/tmp/refresh/shopify_orders_by_channel_30d.json` / `_7d.json`, reshaped
to a plain dict `{"retail": N, "wholesale": N, "faire": N}` (drop the
`exclude_amazon_sync` row; default any missing channel to 0).

`scripts/build_shopify.py` maps each `shopify_sku` to a tracked Amazon SKU
via its `SHOPIFY_SKU_MAP` constant (best-effort, built from observed
product titles — not a guaranteed-exhaustive catalog match). If a *new*
Shopify SKU shows up carrying real product revenue in the `otherUntracked`
bucket (check `DATA.shopify.otherUntracked30` after a refresh), add it to
that map rather than leaving it unattributed. Bundles and the "Shipping
Protection" add-on are expected to stay unmapped — that's correct, not a bug.

## 3. Run the transform + splice scripts

```
cd /tmp/gh_repo && python3 scripts/transform.py && python3 scripts/splice.py
```

`transform.py`:
- Builds the core dashboard data from the step 2a pulls (unchanged behavior).
- Carries forward `inventory` (TFL 3PL data), each SKU's `cogsPerUnit`/`cogsAsOf`,
  and `marginMeta` unchanged from the *current* `/tmp/gh_repo/index.html`
  (cloned in step 0) — these are NOT re-derived automatically because
  `loadTflInventory`'s SKU aliases don't reliably match current Amazon SKU
  codes, and COGS is client-supplied, not pulled from an API. If Tony sends
  updated COGS or 3PL data, that must be applied by an interactive session,
  not this automated refresh.
- Calls `scripts/build_sellerboard.py` on the step 2b raw pulls to build the
  `sellerboard` block (Profitability tab). **If `sb_raw_30d.json`/`sb_raw_7d.json`
  are missing, it logs a warning and carries forward the previous Profitability
  tab data unchanged** rather than failing the whole run — a Sellerboard outage
  should not take down the rest of the refresh.
- Calls `scripts/build_extras.py` on the step 2c/2d/2e raw pulls to build the
  `wow`, `sqpBySku`, and `monthlyTrend` blocks. Same graceful-fallback behavior
  if those raw files are missing.
- Calls `scripts/build_shopify.py` on the step 2f raw pulls to build the
  `shopify` block (Omnichannel tab). Same graceful-fallback behavior if those
  raw files are missing.

`splice.py` writes the new JSON blob into `/tmp/gh_repo/index.html` in place
and prints `VALID JSON, skus: N` — confirm N matches the prior SKU count
(currently 26) before proceeding. If it doesn't match or either script errors,
STOP and do not push — leave the repo as-is and report the failure.

## 4. Sanity check (quick, no full Playwright required)

```
python3 -c "
import re, json
html = open('/tmp/gh_repo/index.html').read()
m = re.search(r'<script id=\"metrics-data\" type=\"application/json\">(.*?)</script>', html, re.S)
d = json.loads(m.group(1))
assert len(d['skus']) >= 20, 'suspiciously few SKUs'
assert d['account']['totalSales'] > 0
assert 'sellerboard' in d and len(d['sellerboard']['products30']) >= 20, 'sellerboard data missing/thin'
assert 'wow' in d and d['wow']['currentWeek']['totalSales'] > 0
assert 'monthlyTrend' in d and len(d['monthlyTrend']) >= 1
assert 'sqpBySku' in d and len(d['sqpBySku']) >= 20
assert 'shopify' in d and len(d['shopify']['bySku30']) >= 20, 'shopify data missing/thin'
print('OK', d['meta'])
"
```

If any assertion fails, STOP — do not push. The graceful-fallback behavior in
step 3 means a genuinely broken pull will usually show up here as "thin" data
rather than a crash, so don't skip this check.

## 5. Commit and push

```
cd /tmp/gh_repo
git add index.html
git -c user.email="tony@sellersedgegroup.com" -c user.name="TK Health auto-refresh" \
  commit -m "Automated data refresh: <dateFirst> - <dateLast>"

env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY \
    -u GIT_CONFIG_COUNT -u GIT_CONFIG_KEY_0 -u GIT_CONFIG_KEY_1 -u GIT_CONFIG_KEY_2 \
    -u GIT_CONFIG_VALUE_0 -u GIT_CONFIG_VALUE_1 -u GIT_CONFIG_VALUE_2 \
    git push origin main
```

(The `env -u ...` prefix bypasses this sandbox's internal git credential
proxy, which otherwise rejects pushes to repos outside its own authorized
set — it forces git to use the PAT embedded in the remote URL instead.)

Netlify is connected to this repo's `main` branch and auto-deploys on every
push — no separate Netlify step is needed. Live URL:
https://tk-health-dashboard.netlify.app/

## If anything fails

Do not push a broken or partial `index.html`. Leave the repo untouched and
report what failed — the live site keeps serving the last good version
either way, so a failed refresh is not urgent to fix by hand.

## Script reference

- `scripts/transform.py` — orchestrator; builds the core dashboard JSON and
  calls the two modules below.
- `scripts/build_sellerboard.py` — aggregates raw Sellerboard product rows
  (which can have multiple legacy SKU codes per ASIN) into one row per ASIN,
  cross-referencing category/TFL data from the live dashboard. Exposes a single
  `build(raw_rows, cur_skus_by_asin, cur_inventory_by_sku)` function.
- `scripts/build_extras.py` — builds `wow`, `sqpBySku`, `monthlyTrend` from
  their respective raw pulls. Exposes `build(account7d, meta7d, tracked_asins)`.
- `scripts/build_shopify.py` — builds the `shopify` block (Amazon + Shopify
  Omnichannel tab: retail/wholesale/Faire channel totals and per-SKU
  breakdown) from the step 2f raw pulls, using its `SHOPIFY_SKU_MAP` constant
  to attribute Shopify line items to tracked Amazon SKUs. Exposes
  `build(meta30, meta7)`.
- `scripts/splice.py` — unchanged; regex-splices the final JSON blob into
  `index.html`'s `#metrics-data` script tag.
