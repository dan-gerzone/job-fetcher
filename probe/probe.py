#!/usr/bin/env python3
"""One-off discovery probe. Writes probe/out.json."""
import concurrent.futures as cf, json, os, re, ssl, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CTX = ssl.create_default_context()

def get(url, data=None, timeout=20, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, application/xml, */*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    if data is not None:
        data = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout, context=CTX) as r:
            return r.status, r.geturl(), r.read(8000000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try: b = e.read(2000).decode("utf-8", "replace")
        except Exception: b = ""
        return e.code, url, b
    except Exception as e:
        return 0, url, type(e).__name__ + ": " + str(e)[:80]

def sq(s, n=600): return re.sub(r"\s+", " ", s)[:n]

import http.cookiejar
def session():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=CTX)), cj

def sget(op, url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, */*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    if data is not None:
        data = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    try:
        with op.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(8000000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(1500).decode("utf-8", "replace")
    except Exception as e:
        return 0, {}, str(e)[:100]

o = {}
def rec(name, url, n=900, find=None, **kw):
    st, fin, b = get(url, **kw)
    i = b.find(find) if find else 0
    o[name] = [st, fin[:90], len(b), sq(b[max(0, i - 150):], n)]
    return b

# --- Getro detail
b = rec("getro_detail", "https://jobs.oakhcft.com/companies/curana-health/jobs/99958716", n=200)
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', b, re.S)
if m:
    cj0 = (json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("initialState", {}).get("jobs", {}) or {}).get("currentJob") or {}
    o["getro_detail_keys"] = [list(cj0.keys())[:40], sq(str(cj0.get("description"))[:300], 300)]
st, fin, bb = get("https://api.getro.com/api/v2/collections/637/search/jobs", data={"hitsPerPage": 100, "page": 0, "filters": {}, "query": "director"}, headers={"Accept": "application/json"})
try:
    jj = json.loads(bb); o["getro_count"] = [st, len(jj["results"]["jobs"]), jj["results"].get("count"), [k for k in jj["results"].keys()]]
except Exception as e: o["getro_count"] = [st, str(e)]
# --- Consider
def consider(host, board, query):
    op, cj = session()
    st, hd, b = sget(op, f"https://{host}/jobs")
    m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', b) or re.search(r"csrfToken[\"']?\s*[:=]\s*[\"']([^\"']+)", b)
    bid = re.search(r'"board"\s*:\s*\{[^{}]*?"id"\s*:\s*"([^"]+)"', b)
    tok = m.group(1) if m else ""
    st, hd2, b2 = sget(op, f"https://{host}/api-boards/search-jobs", data={"meta": {"size": 5}, "board": {"id": board or (bid.group(1) if bid else ""), "isParent": True}, "query": query},
                       headers={"x-csrf-token": tok, "Origin": f"https://{host}", "Referer": f"https://{host}/jobs"})
    return st, b2, (bid.group(1) if bid else None), bool(tok)
st, b2, bid, tk = consider("jobs.gv.com", "gv", {"promoteFeatured": False})
try:
    jj = json.loads(b2); x = jj["jobs"][0]; x.pop("companyLogos", None)
    o["consider_job"] = [list(jj.keys()), jj.get("total"), sq(json.dumps(x), 1700)]
except Exception as e: o["consider_job"] = [st, str(e), sq(b2, 200)]
for nm, q in (("text", {"promoteFeatured": False, "text": "director"}), ("q", {"promoteFeatured": False, "q": "director"}), ("keyword", {"promoteFeatured": False, "keywords": "director"}),
              ("markets", {"promoteFeatured": False, "markets": ["Healthcare"]}), ("titles", {"promoteFeatured": False, "jobTitle": "director"})):
    st, b2, _, _ = consider("jobs.gv.com", "gv", q)
    try:
        jj = json.loads(b2); o[f"consider_q_{nm}"] = [st, jj.get("total"), [(x.get("title"), x.get("companyName")) for x in jj.get("jobs", [])[:5]]]
    except Exception as e: o[f"consider_q_{nm}"] = [st, sq(b2, 200)]
for host in ("jobs.a16z.com", "jobs.healthtechnerds.com", "jobs.greylock.com"):
    st, b2, bid, tk = consider(host, None, {"promoteFeatured": False})
    o[f"consider_{host}"] = [st, bid, tk, sq(b2, 200)]
b = rec("htn", "https://jobs.healthtechnerds.com/", n=100)
i = b.lower().find("ashby"); o["htn_ashby_ctx"] = sq(b[max(0, i - 1200): i + 600], 1800)
o["htn_counts"] = {k: b.lower().count(k) for k in ("ashbyhq", "greenhouse", "lever.co", "<article", "job-card", "data-job", "fetch(", ".json", "api/")}
o["htn_fetch"] = re.findall(r"fetch\(([^)]{0,120})\)", b)[:6]
# --- RSS boards
def item(b):
    i = b.find("<item>"); j = b.find("</item>", i)
    return sq(b[i:j + 7], 1100) if i >= 0 else sq(b, 200)
for host in ("careers.amga.org", "careers.apha.org", "careers.shsmd.org", "jobs.healthcareercenter.com", "jobmine.himss.org", "careers.ache.org", "careercenter.nahq.org", "jobbank.hfma.org", "careers.hfma.org", "careercenter.amia.org",
             "careers.academyhealth.org", "careercenter.ahima.org", "careers.aapc.com", "careercenter.ispor.org", "jobs.amstat.org", "jobs.ashp.org", "careers.aonl.org", "careers.aapl.org", "careers.acpe.org", "jobs.healthcaresource.com",
             "careers.nejm.org", "careers.hcanj.org", "careers.aamc.org", "careers.asahq.org", "careers.aha.org", "careers.namss.org", "careers.acmanet.org", "careers.aone.org", "careers.amcp.org", "careers.ahcancal.org", "careers.chausa.org",
             "careers.vizientinc.com", "careers.essentialhospitals.org", "careers.hcca-info.org", "careers.aahpm.org", "careers.nahc.org", "careers.leadingage.org", "careers.acep.org", "careers.aafp.org", "careers.pcpcc.org", "careers.naacos.com",
             "careers.aana.com", "careers.aanp.org", "careers.amcp.org", "careers.nahu.org", "careers.soa.org", "careers.actuary.org", "careers.amwa.org", "careers.shrm.org", "careers.tdwi.org", "careers.dama.org", "careers.amstat.org"):
    st, fin, b = get(f"https://{host}/jobs/?keywords=director&display=rss", timeout=12)
    if st == 200 and "<rss" in b[:400]:
        o[f"rss_{host}"] = [b.count("<item>"), item(b) if host in ("careers.amga.org", "jobs.healthcareercenter.com") else sq((re.search(r"<title>(.*?)</title>", b, re.S) or [0, ""])[1], 80)]
    else:
        st2, fin2, b2 = get(f"https://{host}/jobsrss/?Keywords=director", timeout=12)
        if st2 == 200 and "<rss" in b2[:400]:
            o[f"madgex_{host}"] = [b2.count("<item>"), sq((re.search(r"<title>(.*?)</title>", b2, re.S) or [0, ""])[1], 80)]
        else:
            o.setdefault("rss_dead", []).append(f"{host}:{st}/{st2}")
st, fin, b = get("https://careers.mgma.com/jobsrss/?Keywords=director")
o["madgex_item"] = item(b)
st, fin, b = get("https://careers.naccho.org/jobs?keywords=director")
i = b.find('data-testid="jobs-list-item-link"'); o["ws_item"] = sq(b[i - 200: i + 2300], 2300)
o["ws_pages"] = re.findall(r'href="([^"]*[?&]page=\d+[^"]*)"', b)[:3]
json.dump(o, open(os.path.join(HERE, "out.json"), "w"), indent=0)
