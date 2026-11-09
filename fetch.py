#!/usr/bin/env python3
"""Fetch job listings from every configured source and write a pre-filtered candidate file.

Outputs (in data/):
  candidates.json  - listings that pass the mechanical title/location filter, with
                     pay snippets and a description excerpt for Claude to judge
  all_ids.json     - every posting id seen per source family (used to detect closures)
  run.json         - run timestamp, per-source counts and errors

Standard library only, so the GitHub Action needs no installs.
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
OUT = os.path.join(ROOT, "data")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

F = CFG["filters"]
RE_LEVEL = re.compile(F["level"], re.I)
RE_LEVEL_G = re.compile(F["levelGoogleExtra"], re.I)
RE_DROP = re.compile(F["dropLevel"], re.I)
RE_EXCL = re.compile(F["excludeTitle"], re.I)
RE_CO = re.compile(r"\bcolorado\b|\bdenver\b|\bboulder\b|,\s*CO\b|\bCO\s*-|-CO\b|\bRemote-CO\b|Work At Home-Colorado", re.I)
RE_REMOTE = re.compile(r"remote|work from home|work at home|telecommut|virtual|anywhere", re.I)
RE_US_ONLY = re.compile(r"^\s*(united states( of america)?|usa?|us)\s*$", re.I)
RE_PAY = re.compile(r"[^.\n]{0,90}\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?[^.\n]{0,110}")
RE_ELIG = re.compile(r"[^.\n]{0,160}(?:residing in|reside in|open to candidates|remote (?:in|from)|eligible states|following states|colorado)[^.\n]{0,220}", re.I)

ERRORS = {}
COUNTS = {}
ALL_IDS = {}   # family -> set(doc ids)


# ---------------------------------------------------------------- helpers
def http(url, data=None, headers=None, tries=4, timeout=40):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            code = getattr(e, "code", None)
            if code in (400, 401, 403, 404, 410, 422):
                break
            if code == 429:
                ra = (getattr(e, "headers", None) or {}).get("Retry-After") or ""
                time.sleep(min(int(ra), 30) if ra.isdigit() else 8 * (i + 1))
            else:
                time.sleep(2 * (i + 1))
    raise last


def jget(url, **kw):
    return json.loads(http(url, **kw))


def text_of(s):
    """HTML (possibly entity-escaped twice) -> plain text."""
    if not s:
        return ""
    s = html.unescape(s)
    if "&lt;" in s or "&amp;" in s:
        s = html.unescape(s)
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def pay_snips(t):
    out = []
    for m in RE_PAY.findall(t or ""):
        m = m.strip()
        if re.search(r"\$\s?\d{1,3}(,\d{3})+|\$\s?\d{2,3}\s?[kK]\b|\$\d{2,3}(\.\d)?\s?[kK]", m) and not re.search(r"million|billion|\$\s?\d+\s?[MB]\b|raised|funding|valuation|revenue|stipend|budget|savings", m, re.I):
            if m not in out:
                out.append(m)
    return out[:3]


def elig_snips(t):
    out = []
    for m in RE_ELIG.findall(t or ""):
        m = m.strip()
        if m not in out:
            out.append(m)
    return out[:3]


def excerpt(t, n=1400):
    t = (t or "").strip()
    return t[:n]


def title_ok(title, google=False):
    if not title:
        return False
    lvl = RE_LEVEL.search(title) or (google and RE_LEVEL_G.search(title))
    return bool(lvl) and not RE_DROP.search(title) and not RE_EXCL.search(title)


def loc_ok(locations, remote=None, state_restricted=False):
    """Mechanical location screen. Return False only when clearly out of bounds;
    borderline cases go through for Claude to judge."""
    locs = [l for l in (locations or []) if l]
    joined = " ; ".join(locs)
    if RE_CO.search(joined):
        return True
    if state_restricted:
        # e.g. Centene "Remote-MO; Remote-TX": state-restricted remote without CO
        if re.search(r"remote-[a-z]{2}\b", joined, re.I) and not re.search(r"nationwide|united states|\bus\b|usa", joined, re.I):
            return False
    if remote is True:
        return True
    if RE_REMOTE.search(joined):
        return True
    if any(RE_US_ONLY.match(l) for l in locs):
        return True   # "United States" alone usually means remote/flexible; let Claude check
    if not locs and remote is None:
        return True
    return False


def norm_date(s):
    if not s:
        return ""
    s = str(s)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{10,13}", s):
        n = int(s)
        if n > 1e12:
            n /= 1000
        return dt.datetime.utcfromtimestamp(n).strftime("%Y-%m-%d")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(re.sub(r"\s+", " ", s.strip()), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s[:20]


def cand(**kw):
    kw.setdefault("pay", [])
    kw.setdefault("eligibility", [])
    kw.setdefault("excerpt", "")
    kw["locations"] = [l for l in kw.get("locations", []) if l][:60]
    return kw


def note_ids(family, ids):
    ALL_IDS.setdefault(family, set()).update(ids)


def run_source(key, fn):
    t0 = time.time()
    try:
        res = fn()
        COUNTS[key] = {"candidates": len(res), "secs": round(time.time() - t0, 1)}
        return res
    except Exception as e:  # noqa
        ERRORS[key] = f"{type(e).__name__}: {e}"[:300]
        return []


# ---------------------------------------------------------------- sources
def greenhouse(slug):
    j = jget(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out, ids = [], []
    for x in j.get("jobs", []):
        did = f"gh-{x['id']}"
        ids.append(did)
        if not title_ok(x.get("title")):
            continue
        loc = (x.get("location") or {}).get("name", "")
        offices = [o.get("name", "") for o in x.get("offices", []) or []]
        t = text_of(x.get("content"))
        locs = [loc] + offices
        remote = bool(RE_REMOTE.search(loc)) or None
        if not loc_ok(locs, remote):
            continue
        out.append(cand(id=did, source="greenhouse", board=slug, company=(x.get("company_name") or slug),
                        title=x["title"], locations=locs, url=x.get("absolute_url"),
                        posted=norm_date(x.get("first_published") or x.get("updated_at")),
                        pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    note_ids("gh", ids)
    COUNTS[f"greenhouse:{slug}:all"] = len(ids)
    return out


def lever(slug):
    j = jget(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out, ids = [], []
    for x in j:
        did = f"lv-{x['id']}"
        ids.append(did)
        if not title_ok(x.get("text")):
            continue
        c = x.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        wt = (x.get("workplaceType") or "").lower()
        remote = True if wt == "remote" else (False if wt in ("onsite", "on-site") else None)
        if not loc_ok(locs, remote):
            continue
        t = text_of((x.get("description") or "") + " " + " ".join(l.get("content", "") for l in x.get("lists", []) or []) + " " + (x.get("additional") or ""))
        pay = pay_snips(t)
        sr = x.get("salaryRange")
        if sr and sr.get("min"):
            pay.insert(0, f"salaryRange ${sr['min']:,} - ${sr.get('max', 0):,} {sr.get('interval', '')}")
        out.append(cand(id=did, source="lever", board=slug, company=slug, title=x["text"], locations=locs,
                        workplace=wt, url=x.get("hostedUrl"), posted=norm_date(x.get("createdAt")),
                        pay=pay, eligibility=elig_snips(t), excerpt=excerpt(t)))
    note_ids("lv", ids)
    COUNTS[f"lever:{slug}:all"] = len(ids)
    return out


def ashby(slug):
    j = jget(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out, ids = [], []
    for x in j.get("jobs", []) or []:
        if x.get("isListed") is False:
            continue
        did = f"ab-{x['id']}"
        ids.append(did)
        if not title_ok(x.get("title")):
            continue
        locs = [x.get("location", "")] + [l.get("location", "") for l in x.get("secondaryLocations", []) or []]
        wt = (x.get("workplaceType") or "").lower()
        remote = True if (x.get("isRemote") and wt != "onsite") else (False if wt == "onsite" else None)
        if not loc_ok(locs, remote):
            continue
        t = x.get("descriptionPlain") or text_of(x.get("descriptionHtml"))
        pay = pay_snips(t)
        comp = (x.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            pay.insert(0, comp)
        out.append(cand(id=did, source="ashby", board=slug, company=slug, title=x["title"], locations=locs,
                        workplace=wt, url=x.get("jobUrl"), posted=norm_date(x.get("publishedAt")),
                        pay=pay, eligibility=elig_snips(t), excerpt=excerpt(t)))
    note_ids("ab", ids)
    COUNTS[f"ashby:{slug}:all"] = len(ids)
    return out


def workable(acct):
    account, company = acct["account"], acct["company"]
    jobs = []
    # v3 is paginated; fall back to the widget API (max ~100 jobs)
    try:
        token, seen = None, 0
        while True:
            body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
            if token:
                body["token"] = token
            j = jget(f"https://apply.workable.com/api/v3/accounts/{account}/jobs", data=body)
            for x in j.get("results", []):
                loc = x.get("location") or {}
                jobs.append({"shortcode": x["shortcode"], "title": x.get("title"),
                             "remote": bool(x.get("remote")) or (x.get("workplace") == "remote"),
                             "country": loc.get("country"), "city": loc.get("city"), "region": loc.get("region"),
                             "locations": [", ".join(filter(None, [l.get("city"), l.get("region"), l.get("country")])) for l in x.get("locations", []) or []],
                             "published": x.get("published")})
            token = j.get("nextPage")
            seen += 1
            if not token or seen > 20:
                break
    except Exception:
        jobs = []
    if not jobs:
        j = jget(f"https://apply.workable.com/api/v1/widget/accounts/{account}")
        for x in j.get("jobs", []):
            jobs.append({"shortcode": x["shortcode"], "title": x.get("title"), "remote": bool(x.get("telecommuting")),
                         "country": x.get("country"), "city": x.get("city"), "region": x.get("state"),
                         "locations": [", ".join(filter(None, [l.get("city"), l.get("region"), l.get("country")])) for l in x.get("locations", []) or []],
                         "published": x.get("published_on")})
    ids = list({f"wk-{x['shortcode']}" for x in jobs})
    note_ids("wk", ids)
    COUNTS[f"workable:{account}:all"] = len(ids)
    out, seen = [], set()
    for x in jobs:
        if x["shortcode"] in seen:
            continue
        seen.add(x["shortcode"])
        us = (x.get("country") or "") in ("United States", "US", "USA") or any("United States" in l for l in x["locations"])
        if not (x["remote"] or us) or not title_ok(re.sub(r"^\d+-", "", x.get("title") or "")):
            continue
        locs = [", ".join(filter(None, [x.get("city"), x.get("region"), x.get("country")]))] + x["locations"]
        if not loc_ok(locs, True if x["remote"] else None):
            continue
        t = ""
        try:
            d = jget(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{x['shortcode']}")
            t = text_of(" ".join(str(d.get(k) or "") for k in ("description", "requirements", "benefits")))
        except Exception:
            pass
        out.append(cand(id=f"wk-{x['shortcode']}", source="workable", board=account, company=company,
                        title=re.sub(r"^\d+-", "", x["title"]), locations=locs, workplace="remote" if x["remote"] else "",
                        url=f"https://apply.workable.com/{account}/j/{x['shortcode']}/", posted=norm_date(x.get("published")),
                        pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


RE_DESC_REMOTE = re.compile(r"work virtually|virtual:|fully remote|100% remote|remote position|remote role|"
                            r"work remotely|work from home|work at home|remote[- ]first|remote eligible|"
                            r"remote within the (us|u\.s\.|united states)|remote,? (us|u\.s\.|united states)", re.I)
RE_NATIONWIDE = re.compile(r"anywhere in the (continental )?(us|u\.s\.|united states)|remote role within the (us|united states)|"
                           r"remote (from home )?(anywhere )?(within|across) the (continental )?(us|united states)|nationwide", re.I)
WD_FULL = set()   # Workday prefixes whose complete posting list was read (absence => closed)


def workday(w):
    base = f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}"
    seen, listing = set(), []

    def page(q, off):
        return jget(f"{base}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q})

    def take(jp):
        for x in jp:
            p = x.get("externalPath")
            if p and p not in seen:
                seen.add(p)
                listing.append(x)

    first = page("", 0)
    total = first.get("total") or 0
    if total <= 1500:
        # small enough to read every posting: complete id list, no keyword gaps
        take(first.get("jobPostings") or [])
        for off in range(20, total, 20):
            take(page("", off).get("jobPostings") or [])
        if len(seen) >= total * 0.95:
            WD_FULL.add(w["prefix"])
    else:
        for q in CFG["workdayQueries"]:
            j = page(q, 0)
            take(j.get("jobPostings") or [])
            for off in range(20, min(j.get("total") or 0, 300), 20):
                take(page(q, off).get("jobPostings") or [])
    pre = [x for x in listing if title_ok(x.get("title"))]

    def detail(x):
        j = jget(base + x["externalPath"])
        p = j.get("jobPostingInfo") or {}
        locs = [p.get("location", "")] + list(p.get("additionalLocations") or [])
        t = text_of(p.get("jobDescription"))
        return x, p, locs, t

    out = []
    ids = [f"wd-{w['prefix']}-{x['bulletFields'][0]}" for x in listing if x.get("bulletFields")]
    failed = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for fut in [ex.submit(detail, x) for x in pre]:
            try:
                x, p, locs, t = fut.result()
            except Exception:
                failed += 1
                continue
            did = f"wd-{w['prefix']}-{p.get('jobReqId')}"
            ids.append(did)
            rt = (p.get("remoteType") or "") + " " + " ".join(locs)
            remote = True if (re.search(r"remote|work at home|work from home|virtual", rt, re.I)
                              or RE_DESC_REMOTE.search(t[:4000])) else None
            nationwide = bool(RE_NATIONWIDE.search(t))
            if nationwide:
                locs = locs + ["(description says remote anywhere in the US)"]
            if not loc_ok(locs, remote, w.get("stateRestrictedRemote", False) and not nationwide):
                continue
            if remote and not RE_REMOTE.search(" ".join(locs)) and not nationwide:
                locs = locs + ["(description says remote/virtual)"]
            out.append(cand(id=did, source=f"workday-{w['prefix']}", board=w["tenant"], company=w["company"],
                            title=p.get("title") or x.get("title"), locations=locs, workplace=p.get("remoteType", ""),
                            url=p.get("externalUrl") or f"https://{w['host']}/{w['site']}{x['externalPath']}",
                            posted=norm_date(p.get("startDate")), pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    if failed and failed > len(pre) / 3:
        raise RuntimeError(f"{failed}/{len(pre)} detail requests failed")
    note_ids(f"wd-{w['prefix']}", ids)
    COUNTS[f"workday:{w['prefix']}:all"] = len(listing)
    COUNTS[f"workday:{w['prefix']}:total"] = total
    return out


def uhg():
    host = "https://careers.unitedhealthgroup.com"
    rows = {}
    for q in CFG["uhgQueries"]:
        qs = urllib.parse.urlencode({"ActiveFacetID": 0, "CurrentPage": 1, "RecordsPerPage": 100, "Distance": 50,
                                     "RadiusUnitType": 0, "Keywords": q, "Location": "", "ShowRadius": "False",
                                     "IsPagination": "False", "FacetType": 0, "SearchResultsModuleName": "Search Results",
                                     "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0,
                                     "SearchType": 5, "OrganizationIds": 34088})
        j = jget(f"{host}/search-jobs/results?{qs}", headers={"X-Requested-With": "XMLHttpRequest"})
        for href, inner in re.findall(r'<a[^>]+href="(/job/[^"]+)"[^>]*>(.*?)</a>', j.get("results", ""), re.S):
            m = re.search(r"(\d+)/?$", href)
            if m:
                rows[m.group(1)] = (href, re.sub(r"\s+", " ", text_of(inner)).strip())
    note_ids("uhg", [f"uhg-{k}" for k in rows])
    COUNTS["uhg:all"] = len(rows)
    pre = {k: v for k, v in rows.items() if title_ok(v[1].split(" | ")[0]) and (RE_REMOTE.search(v[1]) or RE_CO.search(v[1]))}

    def detail(k):
        href, label = pre[k]
        raw = http(host + href)
        raw2 = raw.encode().decode("unicode_escape", "ignore") if "\\u003c" in raw else raw
        t = text_of(raw2)
        posted = (re.search(r'"datePosted"\s*:\s*"([^"]+)"', raw) or [None, ""])[1]
        pay = [m for m in re.findall(r"[^.\n]{0,60}\$\d{2,3},\d{3}(?:\.\d+)?\s*(?:-|to)\s*\$\d{2,3},\d{3}[^.\n]{0,40}", t)][:1]
        return k, href, label, t, posted, pay

    out = []
    with cf.ThreadPoolExecutor(6) as ex:
        for fut in [ex.submit(detail, k) for k in pre]:
            try:
                k, href, label, t, posted, pay = fut.result()
            except Exception:
                continue
            parts = [p.strip() for p in label.split(" | ")]
            title = re.sub(r"\s+\d{6,8}$", "", parts[0])
            i = t.find("Primary Responsibilities")
            ex_txt = t[max(0, i - 900): i + 600] if i > 0 else excerpt(t)
            out.append(cand(id=f"uhg-{k}", source="uhg", company="UnitedHealth Group / Optum", title=title,
                            locations=parts[1:], url=host + href, posted=norm_date(posted), pay=pay,
                            eligibility=elig_snips(t), excerpt=ex_txt))
    return out


def zelis():
    host = "https://careers.zelis.com"
    jobs = {}
    for q in CFG["zelisQueries"]:
        t = http(f"{host}/us/en/search-results?keywords={urllib.parse.quote(q)}")
        a = t.find('"eagerLoadRefineSearch":')
        b = t.find(',"jobwidgetsettings"', a)
        if a < 0 or b < 0:
            continue
        d = json.loads(t[a + len('"eagerLoadRefineSearch":'):b])
        for x in (d.get("data") or {}).get("jobs", []):
            jobs[x["jobId"]] = x
    note_ids("zel", [f"zel-{k}" for k in jobs])
    COUNTS["zelis:all"] = len(jobs)
    out = []
    for k, x in jobs.items():
        if not title_ok(x.get("title")):
            continue
        raw = http(f"{host}/us/en/job/{x['jobSeqNo']}")
        locs = list(dict.fromkeys(re.findall(r'"cityStateCountry":"([^"]+)"', raw)))
        desc = (re.search(r'"description":"(.*?)","', raw, re.S) or [None, ""])[1]
        t = text_of(desc.replace('\\"', '"').replace("\\n", "\n"))
        remote = True if RE_REMOTE.search(" ".join(locs) + " " + (x.get("location") or "")) else None
        if not loc_ok(locs or [x.get("location", "")], remote):
            continue
        out.append(cand(id=f"zel-{k}", source="zelis", company="Zelis", title=x["title"], locations=locs,
                        url=f"{host}/us/en/job/{x['jobSeqNo']}", posted=norm_date(x.get("postedDate")),
                        pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def amazon():
    a = CFG["amazon"]
    found = {}
    reqs = [(q, False) for q in a["nationalQueries"]] + [(q, True) for q in a["coloradoQueries"]]
    for q, co in reqs:
        u = f"https://www.amazon.jobs/en/search.json?base_query={urllib.parse.quote(q)}&country=USA&result_limit=100"
        if co:
            u += "&normalized_state_name[]=Colorado"
        for x in jget(u).get("jobs", []):
            found[str(x.get("id_icims") or x.get("id"))] = (x, co)
    note_ids("amz", [f"amz-{k}" for k in found])
    COUNTS["amazon:all"] = len(found)
    out = []
    for k, (x, co) in found.items():
        title = x.get("title", "")
        if not (title_ok(title) or re.search(r"\b(principal|senior manager|sr\.? manager)\b", title, re.I) and not RE_EXCL.search(title)):
            continue
        loc = x.get("normalized_location") or x.get("location") or ""
        if not RE_CO.search(loc) and not RE_REMOTE.search(loc):
            continue
        t = text_of(" ".join(str(x.get(f) or "") for f in ("description", "basic_qualifications", "preferred_qualifications")))
        out.append(cand(id=f"amz-{k}", source="amazon", company="Amazon", title=title, locations=[loc],
                        url="https://www.amazon.jobs" + (x.get("job_path") or ""), posted=norm_date(x.get("posted_date")),
                        pay=pay_snips(t), eligibility=[], excerpt=excerpt(t)))
    return out


def google():
    g = CFG["google"]
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    reqs = [(f"{base}?q={urllib.parse.quote(q)}&location=United%20States&has_remote=true", "Remote eligible (US)")
            for q in g["remoteQueries"]]
    reqs += [(f"{base}?q={urllib.parse.quote(q)}&location={urllib.parse.quote(l)}&target_level=DIRECTOR&target_level=ADVANCED", l)
             for l in g["coloradoLocations"] for q in g["coloradoQueries"]]
    rx = re.compile(r'<a[^>]+href="(jobs/results/(\d+)[^"]*)"[^>]*aria-label="Learn more about ([^"]+)"')
    found = {}
    for u, where in reqs:
        for pg in (1, 2, 3):
            page = http(u + (f"&page={pg}" if pg > 1 else ""), headers={"Accept": "text/html"})
            new, prev = 0, 0
            for m in rx.finditer(page):
                card = text_of(page[prev:m.start()])[-1500:]
                prev = m.end()
                k = m.group(2)
                locm = re.search(r"\bplace\s*(.*?)\s*(?:laptop_windows|bar_chart|info_outline|Learn more|$)", card, re.S)
                locs = [s.strip() for s in (locm.group(1) if locm else "").split(";") if s.strip()]
                if k not in found:
                    new += 1
                    found[k] = {"title": html.unescape(m.group(3)), "href": m.group(1).split("?")[0], "locs": locs,
                                "where": set(), "remote": "Remote eligible" in card}
                found[k]["where"].add(where)
            if not new:
                break
    if not found:
        raise RuntimeError("no Google results parsed (page layout may have changed)")
    note_ids("goog", [f"goog-{k}" for k in found])
    COUNTS["google:all"] = len(found)
    out = []
    for k, x in found.items():
        if not title_ok(x["title"], google=True):
            continue
        t = text_of(http(base + x["href"].split("jobs/results/", 1)[1], headers={"Accept": "text/html"}))
        i = t.find(x["title"])
        body = t[i:i + 6000] if i >= 0 else t[:6000]
        locs = list(x["locs"])
        if x["remote"] or "Remote eligible" in body[:800] or "Remote eligible (US)" in x["where"]:
            locs.append("Remote eligible")
        locs += [w for w in x["where"] if w != "Remote eligible (US)" and w not in locs]
        if not loc_ok(locs, None):
            continue
        out.append(cand(id=f"goog-{k}", source="google", company="Google", title=x["title"], locations=locs,
                        url=base + x["href"].split("jobs/results/", 1)[1], posted="", pay=pay_snips(body),
                        eligibility=elig_snips(body[:1500]), excerpt=excerpt(body, 1600)))
    return out


def microsoft():
    m = CFG["microsoft"]
    host = "https://apply.careers.microsoft.com"
    found = {}
    reqs = [(q, "Denver, Colorado, United States") for q in m["coloradoQueries"]] + [(q, "United States") for q in m["nationalQueries"]]
    last_err = None
    for q, loc in reqs:
        for start in range(0, 100, 10):
            try:
                j = jget(f"{host}/api/pcsx/search?domain=microsoft.com&query={urllib.parse.quote(q)}&location={urllib.parse.quote(loc)}&start={start}&filter_include_remote=1")
            except Exception as e:  # rate limited: keep what we have, move on
                last_err = e
                time.sleep(15)
                break
            ps = (j.get("data") or {}).get("positions") or []
            time.sleep(3)
            for p in ps:
                found[str(p["id"])] = p
            if len(ps) < 10:
                break
    if not found and last_err:
        raise last_err
    if last_err:
        COUNTS["microsoft:partial"] = str(last_err)[:80]
    note_ids("msft", [f"msft-{k}" for k in found])
    COUNTS["microsoft:all"] = len(found)
    out = []
    for k, p in found.items():
        title = p.get("name", "")
        if not title_ok(title):
            continue
        locs = list(p.get("locations") or [])
        wl = p.get("workLocationOption") or ""
        if not loc_ok(locs + ([wl] if wl else []), True if "remote" in wl.lower() else None):
            continue
        t = ""
        try:
            time.sleep(2)
            d = jget(f"{host}/api/pcsx/position_details?position_id={k}&domain=microsoft.com&hl=en")
            dd = d.get("data") or {}
            t = text_of(dd.get("jobDescription") or "")
            ws = " ".join(dd.get("efcustomTextWorkSite") or [])
            if ws:
                wl = f"{wl} ({ws})" if wl else ws
        except Exception:
            pass
        out.append(cand(id=f"msft-{k}", source="microsoft", company="Microsoft", title=title, locations=locs, workplace=wl,
                        url=f"{host}/careers/job/{k}", posted=norm_date(p.get("postedTs")), pay=pay_snips(t),
                        eligibility=[], excerpt=excerpt(t)))
    return out


# ---------------------------------------------------------------- closure check
def check_url(url):
    """Return (status, final_url, body) without raising."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return None, url, ""


def verify_one(item):
    """Is a tracked listing still open? -> 'open' | 'closed' | 'unknown'."""
    did, url = item["id"], item.get("url") or ""
    fam = did.split("-")[0] if not did.startswith("wd-") else "-".join(did.split("-")[:2])
    if did in ALL_IDS.get(fam, set()):
        return "open"
    if fam in ("gh", "lv", "ab", "wk"):
        # full board lists: absent means closed, unless that family had a failed board
        bad = [k for k in ERRORS if k.split(":")[0] == {"gh": "greenhouse", "lv": "lever", "ab": "ashby", "wk": "workable"}[fam]]
        return "unknown" if bad else "closed"
    if fam.startswith("wd-"):
        if fam[3:] in WD_FULL and f"workday:{fam[3:]}" not in ERRORS:
            return "closed"   # complete posting list read and the id is not in it
        m = re.match(r"https://([^/]+)/([^/]+)(/job/.+)$", url)
        if not m:
            return "unknown"
        w = next((w for w in CFG["workday"] if w["host"] == m.group(1)), None)
        if not w:
            return "unknown"
        st, _, body = check_url(f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}{m.group(3)}")
        if st == 200 and "jobPostingInfo" in body:
            return "open"
        return "closed" if st in (403, 404, 410) else "unknown"
    st, final, body = check_url(url)
    if st in (404, 410):
        return "closed"
    if st != 200:
        return "unknown"
    key = did.split("-", 1)[1]
    if key not in final and key not in body:
        return "closed"   # redirected to a search page
    if re.search(r"no longer (available|accepting|open)|position has been filled|job (is )?not found|this job has expired", body, re.I):
        return "closed"
    return "open"


def verify_all(cands, today):
    """Closure check without help from Claude.

    data/history.json remembers every listing ever passed on as a candidate (plus any
    ids in tracked.json, if present). A remembered listing still in today's candidates
    or in its source's id list is open; otherwise its family list / URL is checked.
    Closed listings stay closed and are not re-checked."""
    hpath = os.path.join(OUT, "history.json")
    hist = json.load(open(hpath)) if os.path.exists(hpath) else {}
    for c in cands:
        h = hist.setdefault(c["id"], {"url": c.get("url") or "", "firstSeen": today})
        h["url"] = c.get("url") or h.get("url", "")
        h["lastListed"] = today
        h.pop("closed", None)
    tpath = os.path.join(ROOT, "tracked.json")
    if os.path.exists(tpath):
        for it in json.load(open(tpath)):
            hist.setdefault(it["id"], {"url": it.get("url", ""), "firstSeen": today})
    cand_ids = {c["id"] for c in cands}
    res, todo = {}, []
    for i, h in hist.items():
        if h.get("closed"):
            res[i] = "closed"
        elif i in cand_ids:
            res[i] = "open"
        else:
            todo.append({"id": i, "url": h.get("url", "")})
    with cf.ThreadPoolExecutor(6) as ex:
        for it, st in zip(todo, ex.map(verify_one, todo)):
            res[it["id"]] = st
            if st == "closed":
                hist[it["id"]]["closed"] = today
    json.dump(dict(sorted(hist.items())), open(hpath, "w"), indent=0)
    return res


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = []
    tasks = []
    tasks += [(f"greenhouse:{s}", lambda s=s: greenhouse(s)) for s in CFG["greenhouse"]]
    tasks += [(f"lever:{s}", lambda s=s: lever(s)) for s in CFG["lever"]]
    tasks += [(f"ashby:{s}", lambda s=s: ashby(s)) for s in CFG["ashby"]]
    tasks += [(f"workable:{a['account']}", lambda a=a: workable(a)) for a in CFG["workable"]]
    tasks += [(f"workday:{w['prefix']}", lambda w=w: workday(w)) for w in CFG["workday"]]
    tasks += [("uhg", uhg), ("zelis", zelis), ("amazon", amazon), ("google", google), ("microsoft", microsoft)]
    with cf.ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(run_source, k, fn): k for k, fn in tasks}
        for f in cf.as_completed(futs):
            jobs.extend(f.result())
    uniq = {}
    for j in jobs:
        uniq.setdefault(j["id"], j)
    cands = sorted(uniq.values(), key=lambda j: (j["source"], j.get("company", ""), j["title"]))
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(cands, open(os.path.join(OUT, "candidates.json"), "w"), indent=1)
    json.dump({k: sorted(v) for k, v in sorted(ALL_IDS.items())}, open(os.path.join(OUT, "all_ids.json"), "w"))
    verify = verify_all(cands, now[:10])
    json.dump({"checkedAt": now, "status": verify}, open(os.path.join(OUT, "verify.json"), "w"), indent=1)
    run = {"fetchedAt": now, "sources": len(tasks), "sourcesFailed": len(ERRORS), "candidates": len(cands),
           "errors": ERRORS, "counts": dict(sorted(COUNTS.items()))}
    json.dump(run, open(os.path.join(OUT, "run.json"), "w"), indent=1)
    print(f"{now}: {len(tasks)} sources, {len(ERRORS)} failed, {len(cands)} candidates")
    for k, v in ERRORS.items():
        print(f"  FAILED {k}: {v}")
    # Fail the job only if most sources failed (likely a network problem)
    return 1 if len(ERRORS) > len(tasks) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
