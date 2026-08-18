import re, json

html = open('/tmp/gh_repo/index.html').read()
data = json.load(open('/tmp/refresh/metrics_out.json'))
blob = json.dumps(data, separators=(',', ':'))

pattern = re.compile(r'(<script id="metrics-data" type="application/json">)(.*?)(</script>)', re.S)
new_html, n = pattern.subn(lambda m: m.group(1) + blob + m.group(3), html)
assert n == 1, f"expected 1 replacement, got {n}"

open('/tmp/gh_repo/index.html', 'w').write(new_html)
print('wrote', len(new_html), 'bytes to /tmp/gh_repo/index.html')

# verify round-trip
m = pattern.search(new_html)
check = json.loads(m.group(2))
print('VALID JSON, skus:', len(check['skus']))
