#!/usr/bin/env python3
"""Temporary diagnostics for sources that return nothing. Writes debug/out.txt."""
import json, os, re, sys, traceback, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch as F

OUT = []
def p(*a):
    OUT.append(" ".join(str(x) for x in a))

def sect(name, fn):
    p(f"\n===== {name}")
    try:
        fn()
    except Exception as e:
        p("EXC", type(e).__name__, str(e)[:500])
        p(traceback.format_exc()[-800:])

def google():
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    u = f"{base}?q=health&location=United%20States&has_remote=true"
    st, final, body = F.check_url(u)
    p("status", st, "final", final, "len", len(body))
    p("counts li", body.count("<li"), "jobs/results/", body.count("jobs/results/"), "h3", body.count("<h3"))
    for m in list(re.finditer(r"jobs/results/\d+", body))[:3]:
        p("CTX", repr(body[max(0, m.start()-700):m.start()+300]))
    i = body.find("AF_initDataCallback")
    p("AF_initDataCallback at", i, body.count("AF_initDataCallback"))
    for m in list(re.finditer(r"AF_initDataCallback\(\{key: '([^']+)'", body))[:10]:
        p("  key", m.group(1), "at", m.start())
    j = body.find("ds:1")
    if j >= 0:
        p("ds:1 ctx", repr(body[j-100:j+2500]))
    if not body.count("jobs/results/"):
        p("HEAD", repr(body[:1500]))

def wd(prefix, extra_paths=()):
    w = next(x for x in F.CFG["workday"] if x["prefix"] == prefix)
    base = f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}"
    j = F.jget(f"{base}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    p("total (empty search)", j.get("total"))
    seen = {}
    for q in F.CFG["workdayQueries"]:
        j = F.jget(f"{base}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
        p(f"q={q!r} total={j.get('total')} got={len(j.get('jobPostings') or [])}")
        for x in j.get("jobPostings") or []:
            seen[x.get("externalPath")] = x
    pre = [x for x in seen.values() if F.title_ok(x.get("title"))]
    p("title_ok:", len(pre), "of", len(seen))
    for x in list(seen.values())[:6]:
        p("  sample listing", json.dumps(x)[:300])
    for x in pre[:8]:
        try:
            d = F.jget(base + x["externalPath"])
            pi = d.get("jobPostingInfo") or {}
            locs = [pi.get("location", "")] + list(pi.get("additionalLocations") or [])
            p("  OK", x["title"][:70], "| locs", locs[:5], "| remoteType", pi.get("remoteType"),
              "| loc_ok", F.loc_ok(locs, None, w.get("stateRestrictedRemote", False)))
        except Exception as e:
            p("  DETAIL EXC", x["title"][:70], x["externalPath"], type(e).__name__, str(e)[:200])
    for path in extra_paths:
        st, _, body = F.check_url(base + path)
        p("extra", path, st, body[:300])
        # search by req id
        rid = path.rsplit("_", 1)[-1]
        j = F.jget(f"{base}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": rid})
        p("  search by id", rid, j.get("total"), [x.get("title") for x in j.get("jobPostings") or []][:3])

def microsoft():
    host = "https://apply.careers.microsoft.com"
    j = F.jget(f"{host}/api/pcsx/search?domain=microsoft.com&query=analytics&location={urllib.parse.quote('Denver, Colorado, United States')}&start=0&filter_include_remote=1")
    d = j.get("data") or {}
    p("keys", list(d.keys()), "count", d.get("count"))
    ps = d.get("positions") or []
    for x in ps[:3]:
        p("  pos", json.dumps(x)[:600])
    j = F.jget(f"{host}/api/pcsx/search?domain=microsoft.com&query=director&location=United%20States&start=0&filter_include_remote=1")
    d = j.get("data") or {}
    p("national director count", d.get("count"), [x.get("name") for x in (d.get("positions") or [])][:10])
    if ps:
        k = ps[0]["id"]
        for u in (f"{host}/api/pcsx/position_details?position_id={k}&domain=microsoft.com&hl=en",):
            try:
                dd = F.jget(u)
                p("details keys", list((dd.get("data") or dd).keys())[:40])
                p("details sample", json.dumps(dd)[:1500])
            except Exception as e:
                p("details EXC", u, e)
    # titles across all configured queries
    m = F.CFG["microsoft"]; names = {}
    for q in m["coloradoQueries"]:
        j = F.jget(f"{host}/api/pcsx/search?domain=microsoft.com&query={urllib.parse.quote(q)}&location={urllib.parse.quote('Denver, Colorado, United States')}&start=0&filter_include_remote=1")
        for x in (j.get("data") or {}).get("positions") or []:
            names[x["id"]] = (x.get("name"), x.get("locations"), x.get("workLocationOption"))
    for v in list(names.values())[:40]:
        p("  ", v)

def amazon():
    a = F.CFG["amazon"]
    for q, co in [(q, False) for q in a["nationalQueries"]] + [(q, True) for q in a["coloradoQueries"]]:
        u = f"https://www.amazon.jobs/en/search.json?base_query={urllib.parse.quote(q)}&country=USA&result_limit=100"
        if co:
            u += "&normalized_state_name[]=Colorado"
        j = F.jget(u)
        jobs = j.get("jobs", [])
        locs = {}
        for x in jobs:
            l = x.get("normalized_location") or x.get("location")
            locs[l] = locs.get(l, 0) + 1
        p(f"q={q!r} co={co} hits={j.get('hits')} n={len(jobs)} top locs={sorted(locs.items(), key=lambda t: -t[1])[:6]}")
        for x in jobs:
            t = x.get("title", "")
            if F.title_ok(t) or re.search(r"\b(principal|senior manager|sr\.? manager)\b", t, re.I):
                p("   ", t[:90], "|", x.get("normalized_location") or x.get("location"))

def hc():
    for site in ("healthcatalystcareers", "External", "HealthCatalyst"):
        for host in ("healthcatalyst.wd5.myworkdayjobs.com", "healthcatalyst.wd1.myworkdayjobs.com"):
            try:
                j = F.jget(f"https://{host}/wday/cxs/healthcatalyst/{site}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}, tries=1)
                p(host, site, "total", j.get("total"), [x.get("title") for x in j.get("jobPostings") or []][:20])
            except Exception as e:
                p(host, site, "EXC", str(e)[:120])

def uhg_nopay():
    host = "https://careers.unitedhealthgroup.com"
    raw = F.http(host + "/job/x/director-analytics/34088/102958121872")
    t = F.text_of(raw.encode().decode("unicode_escape", "ignore") if "\\u003c" in raw else raw)
    for m in re.finditer(r"\$", t):
        p("  $ctx", repr(t[max(0, m.start()-150):m.start()+150]))
    p("len", len(t))

sect("google", google)
sect("workday elv", lambda: wd("elv", ["/job/IN-INDIANAPOLIS-220-VIRGINIA-AVE/Staff-VP-Provider-Data-Solutions_JR210482"]))
sect("workday cen", lambda: wd("cen"))
sect("health catalyst", hc)
sect("microsoft", microsoft)
sect("amazon", amazon)
sect("uhg no pay", uhg_nopay)
os.makedirs("debug", exist_ok=True)
open("debug/out.txt", "w").write("\n".join(OUT))
print("\n".join(OUT)[:3000])
