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

# --- Getro
b = rec("getro_page", "https://jobs.oakhcft.com/jobs", n=200)
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', b, re.S)
if m:
    nd = json.loads(m.group(1))
    pp = nd.get("props", {}).get("pageProps", {})
    o["getro_next_keys"] = [list(nd.keys()), list(pp.keys())[:30], nd.get("buildId")]
    init = pp.get("initialState") or {}
    o["getro_init_keys"] = {k: (list(v.keys())[:12] if isinstance(v, dict) else type(v).__name__) for k, v in init.items()} if isinstance(init, dict) else str(type(init))
    jb = (init.get("jobs") or {}) if isinstance(init, dict) else {}
    o["getro_jobs_sample"] = sq(json.dumps(jb), 2200)
    o["getro_network"] = sq(json.dumps(pp.get("network") or {}), 500)
for nm, hd in (("json_only", {"Accept": "application/json"}), ("json_origin", {"Accept": "application/json", "Origin": "https://jobs.oakhcft.com", "Referer": "https://jobs.oakhcft.com/"}),
               ("star", {"Accept": "*/*", "Origin": "https://jobs.oakhcft.com", "Referer": "https://jobs.oakhcft.com/"})):
    for body in ({"hitsPerPage": 3, "page": 0, "filters": {"page": 0}, "query": "director"}, {"hitsPerPage": 3, "page": 0, "filters": {}, "query": ""}):
        st, fin, bb = get("https://api.getro.com/api/v2/collections/637/search/jobs", data=body, headers=hd)
        o[f"getro_api_{nm}_{'q' if body['query'] else 'e'}"] = [st, len(bb), sq(bb, 1800 if st == 200 and "getro_ok" not in o else 150)]
        if st == 200: o["getro_ok"] = nm
# --- Consider
op, cj = session()
st, hd, b = sget(op, "https://jobs.gv.com/jobs")
o["consider_page"] = [st, {k: v[:160] for k, v in hd.items() if k.lower() in ("set-cookie", "x-csrf-token")}, [c.name for c in cj], re.findall(r'[^"\'<>\s]{0,40}csrf[^"\'<>\s]{0,80}', b, re.I)[:6]]
tok = next((c.value for c in cj if "csrf" in c.name.lower() or "xsrf" in c.name.lower()), None)
m = re.search(r'csrf[-_]?token["\']?\s*(?:content=|[:=])\s*["\']([^"\']+)', b, re.I)
tok = tok or (m.group(1) if m else None)
o["consider_tok"] = bool(tok)
for hname in ("x-csrf-token", "csrf-token", "x-xsrf-token"):
    st, hd2, b2 = sget(op, "https://jobs.gv.com/api-boards/search-jobs", data={"meta": {"size": 3}, "board": {"id": "gv", "isParent": True}, "query": {"promoteFeatured": False}},
                       headers={hname: tok or "", "Origin": "https://jobs.gv.com", "Referer": "https://jobs.gv.com/jobs"})
    o[f"consider_api_{hname}"] = [st, sq(b2, 1600 if st == 200 else 150)]
    if st == 200: break
# --- others
b = rec("htn", "https://jobs.healthtechnerds.com/", n=300)
o["htn_fp"] = [re.findall(r'<script[^>]+src="([^"]+)"', b)[:6], re.findall(r'href="([^"]*job[^"]{4,90})"', b)[:6], [k for k in ("pallet", "getro", "consider", "niceboard", "jboard", "smartjobboard", "webflow", "__NEXT_DATA__", "jobboardly", "polymer", "ashby") if k in b.lower()]]
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', b, re.S)
if m: o["htn_next"] = sq(m.group(1), 1500)
for nm, u in (("naylor_amga", "https://careers.amga.org/jobs/?keywords=director"), ("naylor_aha", "https://jobs.healthcareercenter.com/jobs/?keywords=director+analytics"), ("naylor_nahq", "https://careercenter.nahq.org/jobs/?keywords=director"),
              ("naylor_himss", "https://jobmine.himss.org/jobs/?keywords=director"), ("naylor_ache", "https://careers.ache.org/jobs/?keywords=analytics"), ("naylor_apha", "https://careers.apha.org/jobs/?keywords=director")):
    b = rec(nm, u, n=150)
    links = re.findall(r'href="(/jobs?/\d+/[^"]+)"', b)
    i = b.find(links[0]) if links else -1
    o[nm + "_x"] = [len(set(links)), sq(b[max(0, i - 700): i + 1500], 2000 if nm == "naylor_amga" else 300) if i >= 0 else ""]
rec("naylor_amga_rss", "https://careers.amga.org/jobs/?keywords=director&display=rss", n=900)
b = rec("madgex_mgma", "https://careers.mgma.com/jobs/?Keywords=director", n=100)
links = re.findall(r'href="(/job/\d+/[^"]+)"', b); i = b.find(links[0]) if links else -1
o["madgex_mgma_x"] = [len(set(links)), sq(b[max(0, i - 500): i + 1300], 1700) if i >= 0 else ""]
rec("madgex_mgma_rss", "https://careers.mgma.com/jobsrss/?Keywords=director", n=900)
b = rec("ws_naccho", "https://careers.naccho.org/jobs?keywords=director", n=100)
links = re.findall(r'href="(https://careers\.naccho\.org/job/[^"]+)"', b); i = b.find(links[0]) if links else -1
o["ws_naccho_x"] = [len(set(links)), sq(b[max(0, i - 500): i + 1200], 1500) if i >= 0 else ""]
rec("hudson_feed", "https://andrewhudsonsjobslist.com/jobs/feed/", n=1500, find="<item>")
rec("aspph", "https://publichealthjobs.aspph.org/?s=director", n=300)
rec("aspph_feed", "https://publichealthjobs.aspph.org/feed/?post_type=job_listing&s=director", n=900, find="<item>")
rec("healthecareers", "https://www.healthecareers.com/search-jobs/?keyword=director%20analytics&ps=5", n=300)
b = rec("himalayas", "https://himalayas.app/jobs/api/search?q=healthcare%20analytics%20director&country=US", n=100)
try:
    j = json.loads(b); x = j["jobs"][0]; x["description"] = (x.get("description") or "")[:150]
    o["himalayas_job"] = [j.get("totalCount"), sq(json.dumps(x), 1600), [y.get("title") for y in j["jobs"][:12]]]
except Exception as e: o["himalayas_job"] = str(e)
b = rec("jobicy", "https://jobicy.com/api/v2/remote-jobs?count=50&geo=usa&industry=data-science", n=100)
try:
    j = json.loads(b); x = j["jobs"][0]; x["jobDescription"] = x.get("jobDescription", "")[:100]
    o["jobicy_job"] = [len(j["jobs"]), sq(json.dumps(x), 900)]
except Exception as e: o["jobicy_job"] = str(e)
b = rec("remotive", "https://remotive.com/api/remote-jobs?search=healthcare", n=100)
try:
    j = json.loads(b); x = j["jobs"][0]; x["description"] = x.get("description", "")[:100]
    o["remotive_job"] = [len(j["jobs"]), sq(json.dumps(x), 800)]
except Exception as e: o["remotive_job"] = str(e)
json.dump(o, open(os.path.join(HERE, "out.json"), "w"), indent=0)
