#!/usr/bin/env python3
"""One-off: find which job-board system each target employer uses. Writes probe/out.json."""
import concurrent.futures as cf, json, os, re, ssl, urllib.request
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CTX = ssl.create_default_context()
WDN = [1, 2, 3, 5, 10, 12, 101, 102, 103, 104, 105, 106, 107, 108, 501, 502, 503]

def get(url, data=None, timeout=12):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if data is not None:
        data = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout, context=CTX) as r:
            return r.status, r.geturl(), r.read(400000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:
        return 0, url, type(e).__name__

def J(b):
    try: return json.loads(b)
    except Exception: return None

def titles(xs, key):
    return [str(x.get(key, ""))[:50] for x in xs[:3]]

def probe(slug):
    hits = []
    st, _, b = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"); j = J(b)
    if st == 200 and j and j.get("jobs"):
        n = J(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")[2]) or {}
        hits.append(["greenhouse", slug, len(j["jobs"]), n.get("name"), titles(j["jobs"], "title")])
    st, _, b = get(f"https://api.lever.co/v0/postings/{slug}?mode=json"); j = J(b)
    if st == 200 and isinstance(j, list) and j:
        hits.append(["lever", slug, len(j), None, titles(j, "text")])
    st, _, b = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"); j = J(b)
    if st == 200 and j and j.get("jobs"):
        hits.append(["ashby", slug, len(j["jobs"]), None, titles(j["jobs"], "title")])
    st, _, b = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=3"); j = J(b)
    if st == 200 and j and j.get("totalFound"):
        c = (j["content"][0].get("company") or {}).get("name") if j.get("content") else None
        hits.append(["smartrecruiters", slug, j["totalFound"], c, titles(j.get("content") or [], "name")])
    st, _, b = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"); j = J(b)
    if st == 200 and j and j.get("jobs"):
        hits.append(["workable", slug, len(j["jobs"]), j.get("name"), titles(j["jobs"], "title")])
    st, _, b = get(f"https://{slug}.bamboohr.com/careers/list"); j = J(b)
    if st == 200 and j and j.get("result"):
        hits.append(["bamboohr", slug, len(j["result"]), None, titles(j["result"], "jobOpeningName")])
    st, _, b = get(f"https://{slug}.recruitee.com/api/offers/"); j = J(b)
    if st == 200 and j and j.get("offers"):
        hits.append(["recruitee", slug, len(j["offers"]), None, titles(j["offers"], "title")])
    st, _, b = get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"); j = J(b)
    if st == 200 and isinstance(j, list) and j:
        hits.append(["rippling", slug, len(j), None, titles(j, "name")])
    st, fin, b = get(f"https://jobs.jobvite.com/{slug}/jobs")
    if st == 200 and "jv-job-list" in b:
        hits.append(["jobvite", slug, b.count("jv-job-list-name"), None, []])
    st, fin, b = get(f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1")
    if st == 200 and "icims" in b.lower() and "/jobs/" in b:
        hits.append(["icims", f"careers-{slug}", len(set(re.findall(r"/jobs/(\d+)/", b))), None, []])
    for n in WDN:
        host = f"{slug}.wd{n}.myworkdayjobs.com"
        st, fin, b = get(f"https://{host}/robots.txt", timeout=8)
        if st == 200 and "sitemap" in b.lower():
            sites = sorted(set(re.findall(r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/\s]+)/siteMap", b, re.I)))
            res = []
            for s in sites[:12]:
                st2, _, b2 = get(f"https://{host}/wday/cxs/{slug}/{s}/jobs", data={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
                j2 = J(b2) or {}
                res.append([s, st2, j2.get("total")])
            hits.append(["workday", host, None, None, res])
            break
        if st == 200 and b:
            hits.append(["workday?", host, None, None, [b[:200]]])
    return hits

T = {}
for line in open(os.path.join(os.path.dirname(__file__), "targets.txt")):
    line = line.strip()
    if not line or line.startswith("#"): continue
    name, slugs = line.split(":", 1)
    T[name.strip()] = slugs.split()
pairs = [(n, s) for n, ss in T.items() for s in ss]
out = {n: [] for n in T}
with cf.ThreadPoolExecutor(24) as ex:
    for (n, s), h in zip(pairs, ex.map(lambda p: probe(p[1]), pairs)):
        out[n] += h
json.dump(out, open(os.path.join(os.path.dirname(__file__), "out.json"), "w"), indent=0)
print(sum(1 for v in out.values() if v), "of", len(out), "with hits")
