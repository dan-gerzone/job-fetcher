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
RE_FUNC = re.compile(F.get("broadFunction", "."), re.I)
BROAD = set(CFG.get("broadBoards", []))
NEAR = []       # senior titles dropped by the function or location screen (for auditing the filter)


def near(company, title, reason, locs=(), url=""):
    if len(NEAR) < 4000:
        NEAR.append({"company": company, "title": title, "why": reason, "locations": [l for l in locs if l][:4], "url": url})
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


def title_ok(title, google=False, broad=False):
    """broad=True: big general employers (health systems, med tech, consultancies) must
    also have a relevant function word in the title."""
    if not title:
        return False
    if broad and not RE_FUNC.search(title):
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
        if not title_ok(x.get("title"), broad=slug in BROAD):
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
        if not title_ok(x.get("text"), broad=slug in BROAD):
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
        if not title_ok(x.get("title"), broad=slug in BROAD):
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
    pre = [x for x in listing if title_ok(x.get("title"), broad=w.get("broad", False))]
    if w.get("broad"):
        for x in listing:
            if title_ok(x.get("title")) and not title_ok(x.get("title"), broad=True):
                near(w["company"], x.get("title"), "function", [x.get("locationsText", "")], f"https://{w['host']}/{w['site']}{x['externalPath']}")

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
                near(w["company"], p.get("title") or x.get("title"), "location", locs, p.get("externalUrl") or "")
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


def talentbrew(tb):
    """Radancy/TalentBrew career sites (same platform as UnitedHealth Group's)."""
    host = "https://" + tb["host"]
    rows = {}
    for q in CFG["talentbrewQueries"]:
        qs = urllib.parse.urlencode({"ActiveFacetID": 0, "CurrentPage": 1, "RecordsPerPage": 100, "Distance": 50,
                                     "RadiusUnitType": 0, "Keywords": q, "Location": "", "ShowRadius": "False",
                                     "IsPagination": "False", "FacetType": 0, "SearchResultsModuleName": "Search Results",
                                     "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0,
                                     "SearchType": 5, "OrganizationIds": tb["org"]})
        j = jget(f"{host}/search-jobs/results?{qs}", headers={"X-Requested-With": "XMLHttpRequest"})
        for href, inner in re.findall(r'<a[^>]+href="(/(?:[a-z]{2}/)?job/[^"]+)"[^>]*>(.*?)</a>', j.get("results", ""), re.S):
            m = re.search(r"/(\d+)/?$", href)
            if not m:
                continue
            h = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", inner, re.S)
            title = re.sub(r"\s+", " ", text_of(h.group(1) if h else inner)).strip()
            lm = re.search(r'class="[^"]*location[^"]*"[^>]*>(.*?)</', inner, re.S)
            loc = re.sub(r"\s+", " ", text_of(lm.group(1))).strip() if lm else ""
            if not h and not lm:
                parts = [p.strip() for p in title.split(" | ")]
                title, loc = parts[0], "; ".join(parts[1:])
            rows[m.group(1)] = (href, title, loc)
    if not rows:
        raise RuntimeError("no results parsed")
    fam = f"tb-{tb['prefix']}"
    note_ids("tb", [f"{fam}-{k}" for k in rows])
    COUNTS[f"talentbrew:{tb['prefix']}:all"] = len(rows)
    pre = {k: v for k, v in rows.items() if title_ok(v[1], broad=tb.get("broad", False))}

    def detail(k):
        href, title, loc = pre[k]
        raw = http(host + href)
        raw2 = raw.encode().decode("unicode_escape", "ignore") if "\\u003c" in raw else raw
        t = text_of(raw2)
        posted = (re.search(r'"datePosted"\s*:\s*"([^"]+)"', raw) or [None, ""])[1]
        locs = [loc] + [f"{a}, {b}" for a, b in re.findall(r'"addressLocality"\s*:\s*"([^"]*)"\s*,\s*"addressRegion"\s*:\s*"([^"]*)"', raw)]
        i = t.find(title)
        body = t[i:] if i >= 0 else t
        remote = True if (re.search(r"TELECOMMUTE", raw) or RE_REMOTE.search(title) or RE_DESC_REMOTE.search(body[:5000])) else None
        return k, href, title, list(dict.fromkeys(locs)), remote, body, posted

    out = []
    with cf.ThreadPoolExecutor(6) as ex:
        for fut in [ex.submit(detail, k) for k in pre]:
            try:
                k, href, title, locs, remote, body, posted = fut.result()
            except Exception:
                continue
            if not loc_ok(locs, remote):
                continue
            if remote and not RE_REMOTE.search(" ".join(locs)):
                locs = locs + ["(posting says remote)"]
            out.append(cand(id=f"{fam}-{k}", source=f"talentbrew-{tb['prefix']}", company=tb["company"], title=title,
                            locations=locs, url=host + href, posted=norm_date(posted), pay=pay_snips(body),
                            eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def smartrecruiters(c):
    slug = c["slug"]
    api = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    posts, off = [], 0
    while True:
        j = jget(f"{api}?limit=100&offset={off}")
        posts += j.get("content") or []
        off += 100
        if off >= (j.get("totalFound") or 0) or off > 3000:
            break
    note_ids("sr", [f"sr-{p['id']}" for p in posts])
    COUNTS[f"smartrecruiters:{slug}:all"] = len(posts)
    out = []
    for p in posts:
        if not title_ok(p.get("name"), broad=c.get("broad", False)):
            continue
        l = p.get("location") or {}
        locs = [l.get("fullLocation") or ", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)]
        remote = True if l.get("remote") else None
        if not loc_ok(locs, remote):
            continue
        d = jget(f"{api}/{p['id']}")
        sec = ((d.get("jobAd") or {}).get("sections") or {})
        t = text_of(" ".join((sec.get(k) or {}).get("text") or "" for k in ("jobDescription", "qualifications", "additionalInformation")))
        if remote:
            locs.append("Remote")
        out.append(cand(id=f"sr-{p['id']}", source="smartrecruiters", board=slug, company=c["company"], title=p["name"],
                        locations=locs, url=d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
                        posted=norm_date(p.get("releasedDate")), pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def bamboohr(c):
    slug = c["slug"]
    base = f"https://{slug}.bamboohr.com/careers"
    rows = jget(f"{base}/list").get("result") or []
    note_ids("bb", [f"bb-{slug}-{r['id']}" for r in rows])
    COUNTS[f"bamboohr:{slug}:all"] = len(rows)
    out = []
    for r in rows:
        if not title_ok(r.get("jobOpeningName")):
            continue
        l = r.get("atsLocation") or r.get("location") or {}
        locs = [", ".join(x for x in (l.get("city"), l.get("state") or l.get("province"), l.get("country")) if x)]
        remote = True if (r.get("isRemote") or str(r.get("locationType")) == "1") else None
        if str(r.get("locationType")) == "2":
            locs.append("Hybrid")
        if not loc_ok(locs, remote):
            continue
        d = (jget(f"{base}/{r['id']}/detail").get("result") or {}).get("jobOpening") or {}
        t = text_of(d.get("description"))
        if d.get("compensation"):
            t = f"Compensation: {d['compensation']}. " + t
        if remote:
            locs.append("Remote")
        out.append(cand(id=f"bb-{slug}-{r['id']}", source="bamboohr", board=slug, company=c["company"], title=r["jobOpeningName"],
                        locations=locs, url=f"{base}/{r['id']}", posted=norm_date(d.get("datePosted")),
                        pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def rippling(c):
    slug = c["slug"]
    api = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    rows = jget(api)
    if not isinstance(rows, list):
        raise RuntimeError("unexpected response")
    seen = {}
    for r in rows:   # one row per location; merge
        k = r.get("uuid") or r.get("id")
        e = seen.setdefault(k, {"name": r.get("name"), "url": r.get("url"), "locs": []})
        wl = r.get("workLocation")
        e["locs"].append(wl.get("label") if isinstance(wl, dict) else str(wl or ""))
    note_ids("rp", [f"rp-{k}" for k in seen])
    COUNTS[f"rippling:{slug}:all"] = len(seen)
    out = []
    for k, e in seen.items():
        if not title_ok(e["name"]) or not loc_ok(e["locs"], None):
            continue
        try:
            d = jget(f"{api}/{k}")
        except Exception:
            d = {}
        desc = d.get("description")
        t = text_of(" ".join(str(v) for v in desc.values()) if isinstance(desc, dict) else str(desc or ""))
        t = (t + " " + text_of(json.dumps(d.get("payRangeDetails") or ""))).strip()
        out.append(cand(id=f"rp-{k}", source="rippling", board=slug, company=c["company"], title=e["name"], locations=e["locs"],
                        url=e["url"] or f"https://ats.rippling.com/{slug}/jobs/{k}", posted=norm_date(d.get("createdOn")),
                        pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def icims(c):
    slug = c["slug"]
    base = f"https://careers-{slug}.icims.com/jobs"
    rows = {}
    for pg in range(0, 40):
        page = http(f"{base}/search?ss=1&in_iframe=1&pr={pg}", headers={"Accept": "text/html"})
        ms = list(re.finditer(r'href="(https?://[^"]*?/jobs/(\d+)/[^"]*?/job)[^"]*"', page))
        new = 0
        for i, m in enumerate(ms):
            k = m.group(2)
            if k in rows:
                continue
            chunk = page[m.end(): ms[i + 1].start() if i + 1 < len(ms) else m.end() + 3000]
            before = page[max(0, m.start() - 1500): m.start()]
            tm = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", chunk, re.S) or re.search(r'title="\s*\d+\s*-\s*([^"]+)"', chunk)
            title = re.sub(r"\s+", " ", text_of(tm.group(1))).strip() if tm else ""
            ctx = re.sub(r"\s+", " ", text_of(before[-900:] + " " + chunk[:1500]))
            lm = re.findall(r"\b[A-Z]{2}-[A-Z]{2}(?:-[A-Za-z .']+)?|\bUS-Remote\b|\bRemote\b", ctx)
            rows[k] = (m.group(1), title, list(dict.fromkeys(lm))[:8])
            new += 1
        if not new:
            break
    if not rows:
        raise RuntimeError("no jobs parsed")
    fam = f"ic-{slug}"
    note_ids("ic", [f"{fam}-{k}" for k in rows])
    COUNTS[f"icims:{slug}:all"] = len(rows)
    out = []
    for k, (url, title, locs) in rows.items():
        if not title_ok(title, broad=c.get("broad", False)):
            continue
        t = text_of(http(url + "?in_iframe=1", headers={"Accept": "text/html"}))
        i = t.find(title)
        body = t[i:] if i >= 0 else t
        locs = list(dict.fromkeys(locs + re.findall(r"\bUS-[A-Z]{2}(?:-[A-Za-z .']+)?|\bUS-Remote\b", body[:1500])))[:10]
        remote = True if (RE_REMOTE.search(title) or RE_REMOTE.search(" ".join(locs)) or RE_DESC_REMOTE.search(body[:5000])) else None
        if not loc_ok(locs, remote):
            continue
        if remote and not RE_REMOTE.search(" ".join(locs)):
            locs.append("(posting says remote)")
        out.append(cand(id=f"{fam}-{k}", source="icims", board=slug, company=c["company"], title=title, locations=locs,
                        url=url, posted="", pay=pay_snips(body), eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def _sample(key, rows, title_key):
    if rows:
        r = rows[0]
        COUNTS[f"{key}:sample"] = str(r.get(title_key) if isinstance(r, dict) else r)[:70]


def paylocity(c):
    base = "https://recruiting.paylocity.com"
    page = http(f"{base}/recruiting/jobs/All/{c['guid']}/x", headers={"Accept": "text/html"})
    m = re.search(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", page, re.S) or re.search(r"window\.pageData\s*=\s*(\{.*\})\s*;", page)
    rows = (json.loads(m.group(1)) if m else {}).get("Jobs") or []
    if not m:
        raise RuntimeError("pageData not found")
    fam = f"pl-{c['prefix']}"
    note_ids("pl", [f"{fam}-{r['JobId']}" for r in rows])
    COUNTS[f"paylocity:{c['prefix']}:all"] = len(rows)
    _sample(f"paylocity:{c['prefix']}", rows, "JobTitle")
    out = []
    for r in rows:
        if not title_ok(r.get("JobTitle")):
            continue
        l = r.get("JobLocation") or {}
        locs = [r.get("LocationName") or "", ", ".join(x for x in (l.get("City"), l.get("State")) if x)]
        remote = True if r.get("IsRemote") else None
        if not loc_ok(locs, remote):
            continue
        url = f"{base}/Recruiting/Jobs/Details/{r['JobId']}"
        t = text_of(http(url, headers={"Accept": "text/html"}))
        i = t.find(r["JobTitle"])
        body = t[i:] if i >= 0 else t
        if remote:
            locs.append("Remote")
        out.append(cand(id=f"{fam}-{r['JobId']}", source="paylocity", company=c["company"], title=r["JobTitle"], locations=locs,
                        url=url, posted=norm_date(r.get("PublishedDate")), pay=pay_snips(body), eligibility=elig_snips(body),
                        excerpt=excerpt(body[:6000])))
    return out


def adp(c):
    api = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    q = f"cid={c['cid']}&ccId=19000101_000001&lang=en_US&locale=en_US"
    rows = jget(f"{api}?{q}&$top=200").get("jobRequisitions") or []
    fam = f"adp-{c['prefix']}"
    note_ids("adp", [f"{fam}-{r.get('itemID')}" for r in rows])
    COUNTS[f"adp:{c['prefix']}:all"] = len(rows)
    _sample(f"adp:{c['prefix']}", rows, "requisitionTitle")
    out = []
    for r in rows:
        title = r.get("requisitionTitle") or ""
        if not title_ok(title):
            continue
        locs = []
        for l in r.get("requisitionLocations") or []:
            a = l.get("address") or {}
            locs.append((l.get("nameCode") or {}).get("shortName") or "")
            locs.append(", ".join(x for x in (a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue")) if x))
        blob = json.dumps(r)
        remote = True if re.search(r"remote|telecommut|virtual", blob, re.I) else None
        if not loc_ok(locs, remote):
            continue
        try:
            d = jget(f"{api}/{r['itemID']}?{q}")
        except Exception:
            d = {}
        t = text_of(d.get("requisitionDescription") or "")
        pay = pay_snips(t) or pay_snips(" ".join(f"${x.get('amountValue'):,.0f}" for x in [((r.get('payGradeRange') or {}).get('minimumRate') or {}), ((r.get('payGradeRange') or {}).get('maximumRate') or {})] if isinstance(x.get('amountValue'), (int, float))))
        post = next((f.get("dateValue") for f in ((r.get("customFieldGroup") or {}).get("dateFields") or []) if (f.get("nameCode") or {}).get("codeValue") == "PostingDate"), r.get("postDate"))
        if remote:
            locs.append("(posting mentions remote)")
        out.append(cand(id=f"{fam}-{r['itemID']}", source="adp", company=c["company"], title=title, locations=locs,
                        url=f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={c['cid']}&ccId=19000101_000001&jobId={r['itemID'].split('_')[0]}&lang=en_US",
                        posted=norm_date(post), pay=pay, eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def ukg(c):
    base = f"https://recruiting2.ultipro.com/{c['board']}"
    rows, skip = [], 0
    while True:
        j = jget(f"{base}/JobBoardView/LoadSearchResults", data={
            "opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": "", "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}], "Filters": []},
            "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}})
        got = j.get("opportunities") or []
        rows += got
        skip += 50
        if len(got) < 50 or skip > 1500:
            break
    fam = f"ukg-{c['prefix']}"
    note_ids("ukg", [f"{fam}-{r['Id']}" for r in rows])
    COUNTS[f"ukg:{c['prefix']}:all"] = len(rows)
    out = []
    for r in rows:
        title = r.get("Title") or ""
        if not title_ok(title):
            continue
        locs = []
        for l in r.get("Locations") or []:
            a = l.get("Address") or {}
            locs.append(", ".join(x for x in (a.get("City"), (a.get("State") or {}).get("Code")) if x))
        remote = True if (RE_REMOTE.search(title) or str(r.get("JobLocationType")) == "3") else None
        if not loc_ok(locs, remote):
            continue
        url = f"{base}/OpportunityDetail?opportunityId={r['Id']}"
        raw = http(url, headers={"Accept": "text/html"})
        m = re.search(r'"Description"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        t = text_of(json.loads('"' + m.group(1) + '"')) if m else text_of(raw)
        t = t or (r.get("BriefDescription") or "")
        if remote:
            locs.append("Remote")
        out.append(cand(id=f"{fam}-{r['Id']}", source="ukg", company=c["company"], title=title, locations=locs, url=url,
                        posted=norm_date(r.get("PostedDate")), pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def oracle(c):
    api = f"https://{c['host']}/hcmRestApi/resources/latest"
    rows = {}
    for q in CFG["keywordQueries"]:
        for off in range(0, 600, 200):
            f = f"findReqs;siteNumber={c['site']},facetsList=NONE,limit=200,offset={off},keyword={urllib.parse.quote(q)},sortBy=POSTING_DATES_DESC"
            j = jget(f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder={f}")
            it = (j.get("items") or [{}])[0]
            got = it.get("requisitionList") or []
            for r in got:
                rows[str(r["Id"])] = r
            if len(got) < 200:
                break
    fam = f"or-{c['prefix']}"
    note_ids("or", [f"{fam}-{k}" for k in rows])
    COUNTS[f"oracle:{c['prefix']}:all"] = len(rows)
    out = []
    for k, r in rows.items():
        title = r.get("Title") or ""
        if not title_ok(title, broad=c.get("broad", False)):
            continue
        locs = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in (r.get("secondaryLocations") or [])]
        wt = str(r.get("WorkplaceType") or r.get("WorkplaceTypeCode") or "")
        remote = True if (re.search(r"remote", wt, re.I) or RE_REMOTE.search(title)) else None
        d = {}
        try:
            fd = urllib.parse.quote(f'ById;Id="{k}",siteNumber={c["site"]}')
            d = (jget(f"{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={fd}").get("items") or [{}])[0]
        except Exception:
            pass
        t = text_of(" ".join(str(d.get(x) or "") for x in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr", "CorporateDescriptionStr")))
        if remote is None and RE_DESC_REMOTE.search(t[:5000]):
            remote = True
        if not loc_ok(locs, remote):
            continue
        if remote and not RE_REMOTE.search(" ".join(locs)):
            locs.append(f"(remote: {wt or 'per posting'})")
        out.append(cand(id=f"{fam}-{k}", source="oracle", company=c["company"], title=title, locations=locs, workplace=wt,
                        url=f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{k}",
                        posted=norm_date(r.get("PostedDate")), pay=pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t)))
    return out


def breezy(c):
    rows = jget(f"https://{c['slug']}.breezy.hr/json")
    note_ids("bz", [f"bz-{r['id']}" for r in rows])
    COUNTS[f"breezy:{c['slug']}:all"] = len(rows)
    out = []
    for r in rows:
        if not title_ok(r.get("name")):
            continue
        l = r.get("location") or {}
        locs = [l.get("name") or ""]
        remote = True if l.get("is_remote") else None
        if not loc_ok(locs, remote):
            continue
        t = text_of(http(r["url"], headers={"Accept": "text/html"}))
        i = t.find(r["name"])
        body = t[i:] if i >= 0 else t
        if remote:
            locs.append("Remote")
        out.append(cand(id=f"bz-{r['id']}", source="breezy", company=c["company"], title=r["name"], locations=locs, url=r["url"],
                        posted=norm_date(r.get("published_date")), pay=([r["salary"]] if r.get("salary") else pay_snips(body)),
                        eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def govjobs(c):
    """NEOGOV (governmentjobs.com) state job board, limited to the departments in c['departments']."""
    base = "https://www.governmentjobs.com"
    dep = re.compile(c["departments"], re.I)
    rows = {}
    for q in ("director", "chief", "analytics", "data"):
        for pg in range(1, 9):
            page = http(f"{base}/careers/home/index?agency={c['agency']}&keyword={q}&sort=PostingDate&isDescendingSort=true&page={pg}",
                        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "text/html"})
            items = re.split(r'<li class="list-item"', page)[1:]
            new = 0
            for it in items:
                m = re.search(r'data-job-id="(\d+)"', it)
                a = re.search(r'class="item-details-link"[^>]*?data-department-name="([^"]*)"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', it, re.S)
                if not m or not a or m.group(1) in rows:
                    continue
                meta = [re.sub(r"\s+", " ", text_of(x)).strip() for x in re.findall(r"<li>(.*?)</li>", (re.search(r'<ul class="list-meta">(.*?)</ul>', it, re.S) or [None, ""])[1], re.S)]
                rows[m.group(1)] = {"dept": html.unescape(a.group(1)), "href": a.group(2), "title": re.sub(r"\s+", " ", text_of(a.group(3))).strip(), "meta": meta}
                new += 1
            if not new:
                break
    fam = f"gj-{c['agency']}"
    note_ids("gj", [f"{fam}-{k}" for k in rows])
    COUNTS[f"govjobs:{c['agency']}:all"] = len(rows)
    out = []
    for k, r in rows.items():
        if not dep.search(r["dept"]) or not title_ok(r["title"]):
            continue
        url = base + r["href"]
        t = text_of(http(url, headers={"Accept": "text/html"}))
        i = t.find("Department Information")
        body = t[i:] if i >= 0 else t
        pay = [m for m in r["meta"] if "$" in m][:1] or pay_snips(body)
        locs = [m for m in r["meta"] if "$" not in m and not m.lower().startswith("department")][:2] or ["Colorado"]
        if not RE_CO.search(" ".join(locs)):
            locs.append("Colorado (state government)")
        out.append(cand(id=f"{fam}-{k}", source="govjobs", company=f"{c['company']} - {r['dept']}", title=r["title"], locations=locs,
                        url=url, posted="", pay=pay, eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def successfactors(c):
    """SAP SuccessFactors career sites (jobs.<company>.com/search/?q=...)."""
    host = "https://" + c["host"]
    rows = {}
    for q in CFG["keywordQueries"]:
        for start in range(0, 250, 25):
            page = http(f"{host}/search/?q={urllib.parse.quote(q)}&sortColumn=referencedate&sortDirection=desc&startrow={start}", headers={"Accept": "text/html"})
            trs = re.split(r'<tr class="data-row', page)[1:]
            for tr in trs:
                a = re.search(r'<a[^>]*href="(/job/[^"]+?/(\d+)/?)"[^>]*class="jobTitle-link"[^>]*>(.*?)</a>', tr, re.S) or \
                    re.search(r'<a[^>]*class="jobTitle-link"[^>]*href="(/job/[^"]+?/(\d+)/?)"[^>]*>(.*?)</a>', tr, re.S)
                if not a:
                    continue
                loc = re.search(r'class="jobLocation">\s*([^<]+?)\s*<', tr)
                date = re.search(r'class="jobDate[^"]*">\s*([^<]+?)\s*<', tr)
                shift = re.search(r'class="jobShifttype">\s*([^<]*?)\s*<', tr)
                rows[a.group(2)] = {"href": html.unescape(a.group(1)), "title": html.unescape(text_of(a.group(3))).strip(),
                                    "loc": html.unescape(loc.group(1)) if loc else "", "date": date.group(1) if date else "",
                                    "shift": shift.group(1) if shift else ""}
            if len(trs) < 25:
                break
    if not rows:
        raise RuntimeError("no rows parsed")
    fam = f"sf-{c['prefix']}"
    note_ids("sf", [f"{fam}-{k}" for k in rows])
    COUNTS[f"successfactors:{c['prefix']}:all"] = len(rows)
    out = []
    for k, r in rows.items():
        if not title_ok(r["title"], broad=c.get("broad", False)):
            continue
        locs = [r["loc"]]
        remote = True if (re.search(r"^any city|remote|virtual", r["loc"] + " " + r["shift"] + " " + r["title"], re.I)) else None
        if remote is None and not RE_CO.search(r["loc"]):
            continue
        t = text_of(http(host + r["href"], headers={"Accept": "text/html"}))
        i = t.find(r["title"])
        body = t[i:] if i >= 0 else t
        if remote:
            locs.append(f"Remote/virtual ({r['shift']})" if r["shift"] else "Remote/virtual")
        out.append(cand(id=f"{fam}-{k}", source="successfactors", company=c["company"], title=r["title"], locations=locs,
                        url=host + r["href"], posted=norm_date(r["date"].strip()), pay=pay_snips(body),
                        eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def jibe(c):
    """iCIMS 'Jibe' career sites (careers.<company>/api/jobs)."""
    host = "https://" + c["host"]
    rows, pg, total = {}, 1, None
    while pg <= 40:
        j = jget(f"{host}/api/jobs?page={pg}&limit=100&sortBy=posted_date&descending=true", headers={"Referer": host + "/jobs"})
        got = j.get("jobs") or []
        for g in got:
            d = g.get("data") or {}
            rows[str(d.get("req_id") or d.get("slug"))] = d
        total = j.get("totalCount") or j.get("count") or total
        pg += 1
        if len(got) < 100:
            break
    fam = f"jb-{c['prefix']}"
    note_ids("jb", [f"{fam}-{k}" for k in rows])
    COUNTS[f"jibe:{c['prefix']}:all"] = len(rows)
    out = []
    for k, d in rows.items():
        title = d.get("title") or ""
        if not title_ok(title, broad=c.get("broad", False)):
            continue
        locs = [d.get("full_location") or d.get("location_name") or "", ", ".join(x for x in (d.get("city"), d.get("state")) if x)]
        locs += [x.get("location_name") or "" for x in (d.get("additional_locations") or []) if isinstance(x, dict)]
        t = text_of((d.get("description") or "") + " " + (d.get("qualifications") or "") + " " + (d.get("responsibilities") or ""))
        tags = json.dumps([d.get(x) for x in ("location_type", "tags1", "tags2", "tags3", "tags4", "employment_type", "categories")])
        remote = True if (re.search(r"remote|virtual|telecommut", tags + " " + title + " " + " ".join(locs), re.I) or RE_DESC_REMOTE.search(t[:5000])) else None
        if not loc_ok(locs, remote):
            continue
        if remote and not RE_REMOTE.search(" ".join(locs)):
            locs.append("(posting says remote)")
        out.append(cand(id=f"{fam}-{k}", source="jibe", company=c["company"], title=title, locations=locs,
                        url=f"{host}/jobs/{d.get('slug') or k}", posted=norm_date(d.get("posted_date") or d.get("create_date")),
                        pay=pay_snips(t + " " + str(d.get("salary_value") or "")), eligibility=elig_snips(t), excerpt=excerpt(t)))
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
# ---------------------------------------------------------------- discovery sources
# These search across many employers (investor portfolio boards, association career centers,
# aggregators). Candidates carry discovery=True and are dropped when a direct source has the same job.
D = CFG.get("discovery", {})
RE_HEALTH = re.compile(D.get("health", "health"), re.I)
RE_HEALTH2 = re.compile(D.get("healthStrong", "health"), re.I)
RE_NOTHEALTH = re.compile(D.get("notHealth", "$^"), re.I)


def healthy(company, title, desc=""):
    """Is this posting plausibly healthcare? Company/title match, or several strong terms in the text."""
    if RE_NOTHEALTH.search(title or ""):
        return False
    if RE_HEALTH.search(f"{company} {title}"):
        return True
    return len(set(m.lower() for m in RE_HEALTH2.findall((desc or "")[:2500]))) >= 3
RE_USA = re.compile(r"united states|\busa?\b|north america|,\s*[A-Z]{2}\b|\b(remote|anywhere)\s*$", re.I)
DISC_FAMS = {"gt", "hm", "rm", "jy", "ny", "mx", "ws", "ah", "htn", "az", "uj"}


def canon_id(url):
    """ATS posting URL -> the id a direct source would use, for de-duplication."""
    u = url or ""
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?[^ ]*token=|[^/]+/jobs/)(\d+)", u) or re.search(r"[?&]gh_jid=(\d+)", u)
    if m:
        return f"gh-{m.group(1)}"
    m = re.search(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})", u)
    if m:
        return f"lv-{m.group(1)}"
    m = re.search(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})", u)
    if m:
        return f"ab-{m.group(1)}"
    m = re.search(r"apply\.workable\.com/[^/]+/j/([A-Z0-9]+)", u)
    if m:
        return f"wk-{m.group(1)}"
    return ""


def describe(url, title):
    """Best-effort posting text for a job found on a discovery board."""
    try:
        m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
        if m:
            return text_of(jget(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", tries=1).get("content"))
        m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            j = jget(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", tries=1)
            return text_of((j.get("descriptionPlain") or "") + " " + " ".join(text_of(x.get("content")) for x in j.get("lists") or []) + " " + (j.get("additionalPlain") or "")
                           + " " + str((j.get("salaryRange") or "")))
        m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
        if m:
            for j in jget(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}?includeCompensation=true", tries=1).get("jobs") or []:
                if j.get("id") == m.group(2):
                    comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                    return ((f"Compensation: {comp}. " if comp else "") + (j.get("descriptionPlain") or text_of(j.get("descriptionHtml"))))
        t = text_of(http(url, headers={"Accept": "text/html"}, tries=1, timeout=20))
        i = t.find(title)
        return t[i:i + 7000] if i >= 0 else t[:5000]
    except Exception:
        return ""


def dcand(**kw):
    kw["discovery"] = True
    return cand(**kw)


def getro(g):
    """Investor portfolio job board (Getro). Keeps only health-tagged companies."""
    api = f"https://api.getro.com/api/v2/collections/{g['id']}/search/jobs"
    rows = {}
    for q in D["titleQueries"]:
        for pg in range(0, 40):
            j = jget(api, data={"hitsPerPage": 20, "page": pg, "filters": {"page": pg}, "query": q}, headers={"Accept": "application/json"})
            got = (j.get("results") or {}).get("jobs") or []
            for r in got:
                rows[r["id"]] = r
            if len(got) < 20:
                break
    note_ids("gt", [f"gt-{k}" for k in rows])
    COUNTS[f"getro:{g['host']}:all"] = len(rows)
    pre = []
    for k, r in rows.items():
        org = r.get("organization") or {}
        if not title_ok(r.get("title"), broad=True):
            continue
        if not RE_HEALTH.search(" ".join(org.get("industry_tags") or []) + " " + (org.get("name") or "")):
            continue
        locs = list(r.get("locations") or [])
        remote = True if r.get("work_mode") == "remote" else None
        if remote and not RE_CO.search(" ".join(locs)):
            rest = [l for l in (r.get("searchable_locations") or locs) if not RE_REMOTE.search(l)]
            if rest and not RE_USA.search(" ; ".join(rest)):
                continue   # remote, but outside the US
        if not loc_ok(locs, remote):
            continue
        pre.append((k, r, org, locs, remote))

    def one(x):
        k, r, org, locs, remote = x
        t = describe(r.get("url") or "", r.get("title") or "")
        pay = []
        lo, hi = r.get("compensation_amount_min_cents"), r.get("compensation_amount_max_cents")
        if lo and hi and (r.get("compensation_period") or "").startswith("year"):
            pay = [f"${lo / 100:,.0f} - ${hi / 100:,.0f} per year (from job board)"]
        return dcand(id=f"gt-{k}", source="getro", board=g["name"], company=org.get("name") or "", title=r["title"],
                     locations=locs + (["Remote"] if remote and not RE_REMOTE.search(" ".join(locs)) else []),
                     url=r.get("url") or f"https://{g['host']}/companies/{org.get('slug')}/jobs/{r.get('slug')}",
                     posted=norm_date(r.get("created_at")), pay=pay or pay_snips(t), eligibility=elig_snips(t), excerpt=excerpt(t),
                     canon=canon_id(r.get("url")))
    with cf.ThreadPoolExecutor(4) as ex:
        return list(ex.map(one, pre))


def _rss_items(xml):
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        f = {}
        for tag in ("title", "link", "description", "pubDate", "guid"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", it, re.S)
            v = m.group(1) if m else ""
            v = re.sub(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", r"\1", v, flags=re.S)
            f[tag] = html.unescape(v).strip()
        m = re.search(r"<content:encoded>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</content:encoded>", it, re.S)
        f["content"] = m.group(1) if m else ""
        out.append(f)
    return out


def _rss_date(s):
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        return ""


def naylor(c):
    """Association career center (Naylor): RSS search results, 50 per query."""
    rows = {}
    for q in D["boardQueries"]:
        xml = http(f"https://{c['host']}/jobs/?keywords={urllib.parse.quote_plus(q)}&display=rss", headers={"Accept": "application/rss+xml, text/xml"})
        for it in _rss_items(xml):
            m = re.search(r"/jobs/(?:rss/)?(\d+)/", it["link"])
            if m:
                rows[m.group(1)] = it
    note_ids("ny", [f"ny-{k}" for k in rows])
    COUNTS[f"naylor:{c['host']}:all"] = len(rows)
    out = []
    for k, it in rows.items():
        title, _, employer = it["title"].rpartition(" | ")
        title = title or it["title"]
        if not title_ok(title, broad=True):
            continue
        desc = text_of(it["description"])
        loc = ", ".join(desc.split(",")[:2]).strip()[:60]
        remote = True if (RE_REMOTE.search(title) or RE_DESC_REMOTE.search(desc[:3000])) else None
        if not loc_ok([loc], remote):
            continue
        out.append(dcand(id=f"ny-{k}", source="naylor", board=c["name"], company=employer.strip(), title=title.strip(),
                         locations=[loc] + (["(posting says remote)"] if remote else []), url=it["link"].replace("/jobs/rss/", "/jobs/"),
                         posted=_rss_date(it["pubDate"]), pay=pay_snips(desc), eligibility=elig_snips(desc), excerpt=excerpt(desc)))
    return out


def madgex(c):
    """Association career center (Madgex): RSS search results."""
    rows = {}
    for q in D["boardQueries"]:
        xml = http(f"https://{c['host']}/jobsrss/?Keywords={urllib.parse.quote_plus(q)}", headers={"Accept": "application/rss+xml, text/xml"})
        for it in _rss_items(xml):
            m = re.search(r"/job/(\d+)/", it["link"])
            if m:
                rows[m.group(1)] = it
    fam = f"mx-{c['prefix']}"
    note_ids("mx", [f"{fam}-{k}" for k in rows])
    COUNTS[f"madgex:{c['prefix']}:all"] = len(rows)
    out = []
    for k, it in rows.items():
        employer, _, title = it["title"].partition(": ")
        if not title:
            employer, title = "", it["title"]
        if not title_ok(title, broad=True):
            continue
        desc = re.sub(r"\s+", " ", text_of(it["description"]))
        m = re.search(r"([A-Z][A-Za-z .'-]+,\s*[A-Z][A-Za-z .]+(?:\s*\(US\))?|Homeworking|Remote[^.]{0,40})\s*$", desc)
        loc = m.group(1).strip() if m else ""
        remote = True if (RE_REMOTE.search(title) or re.search(r"homeworking|remote", loc, re.I) or RE_DESC_REMOTE.search(desc)) else None
        if not loc_ok([loc] if loc else ["unknown location"], remote):
            continue
        out.append(dcand(id=f"{fam}-{k}", source="madgex", board=c["name"], company=employer.strip(), title=title.strip(),
                         locations=[loc] + (["(posting says remote)"] if remote else []), url=it["link"].split("?")[0],
                         posted=_rss_date(it["pubDate"]), pay=pay_snips(desc), eligibility=[], excerpt=excerpt(desc)))
    return out


def webscribble(c):
    """Association career center (Web Scribble): HTML search results."""
    base = f"https://{c['host']}"
    rows = {}
    for q in ("director", "chief", "data", "analytics", "informatics"):
        for pg in range(1, 6):
            page = http(f"{base}/jobs?keywords={q}&page={pg}", headers={"Accept": "text/html"})
            new = 0
            for it in re.split(r'<li\s+class="panel card', page)[1:]:
                a = re.search(r'<a href="([^"]+/job/[^"]+?-(\d+))"[^>]*data-testid="jobs-list-item-link"[^>]*>(.*?)</a>', it, re.S)
                if not a or a.group(2) in rows:
                    continue
                emp = re.search(r'fa-building[^>]*></i>\s*(.*?)\s*</div>', it, re.S)
                loc = re.search(r'fa-location-dot[^>]*></i>\s*(.*?)\s*<', it, re.S)
                sal = re.search(r'Salary</td>\s*<td>(.*?)</td>', it, re.S)
                rows[a.group(2)] = {"url": a.group(1), "title": re.sub(r"\s+", " ", text_of(a.group(3))).strip(), "company": text_of(emp.group(1)) if emp else "",
                                    "loc": re.sub(r"\s+", " ", text_of(loc.group(1))).strip() if loc else "", "pay": text_of(sal.group(1)) if sal else ""}
                new += 1
            if not new:
                break
    fam = f"ws-{c['prefix']}"
    note_ids("ws", [f"{fam}-{k}" for k in rows])
    COUNTS[f"webscribble:{c['prefix']}:all"] = len(rows)
    out = []
    for k, r in rows.items():
        if not title_ok(r["title"]) or not loc_ok([r["loc"]], True if RE_REMOTE.search(r["loc"] + " " + r["title"]) else None):
            continue
        t = text_of(http(r["url"], headers={"Accept": "text/html"}))
        i = t.find(r["title"])
        body = t[i:] if i >= 0 else t
        out.append(dcand(id=f"{fam}-{k}", source="webscribble", board=c["name"], company=r["company"], title=r["title"], locations=[r["loc"]],
                         url=r["url"], posted="", pay=[r["pay"]] if "$" in r["pay"] else pay_snips(body), eligibility=elig_snips(body), excerpt=excerpt(body[:6000])))
    return out


def hudson():
    """Andrew Hudson's Jobs List (Colorado jobs, all fields): keep health-related senior roles."""
    rows = {}
    for pg in range(1, 6):
        try:
            xml = http(f"https://andrewhudsonsjobslist.com/jobs/feed/?paged={pg}", headers={"Accept": "application/rss+xml, text/xml"}, tries=2)
        except Exception:
            if pg == 1:
                raise
            break
        items = _rss_items(xml)
        for it in items:
            slug = it["link"].rstrip("/").rsplit("/", 1)[-1]
            rows[slug] = it
        if not items:
            break
    note_ids("ah", [f"ah-{k}" for k in rows])
    COUNTS["hudson:all"] = len(rows)
    out = []
    for k, it in rows.items():
        body = text_of(it["content"] or it["description"])
        if not title_ok(it["title"], broad=True) or not healthy("", it["title"], body):
            continue
        m = re.search(r"-in-(.+?)-us$", k)
        loc = m.group(1).replace("-", " ").title() if m else "Colorado"
        co = re.search(r"\n\s*([A-Z][^\n]{2,60}?) (?:is|are) (?:looking|seeking|hiring)", "\n" + body)
        out.append(dcand(id=f"ah-{k}", source="hudson", board="Andrew Hudson's Jobs List", company=co.group(1) if co else "(see posting)", title=it["title"],
                         locations=[loc if RE_CO.search(loc) else loc + ", Colorado"], url=it["link"], posted=_rss_date(it["pubDate"]),
                         pay=pay_snips(body), eligibility=[], excerpt=excerpt(body)))
    return out


def htn():
    """Health Tech Nerds community job board (static JSON built from its Slack #jobs channel)."""
    j = jget("https://jobs.healthtechnerds.com/data/transformed_job_data.json")
    rows = j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), [])
    COUNTS["htn:all"] = len(rows)
    out, ids = [], []
    for r in rows:
        d = r.get("job_data") or {}
        title, co, url = d.get("job_title") or "", d.get("company_name") or "", r.get("job_url") or r.get("slack_url") or ""
        rid = "htn-" + str(r.get("slack_ts") or re.sub(r"[^a-z0-9]+", "-", (co + "-" + title).lower())[:70]).replace(".", "")
        ids.append(rid)
        if not title_ok(title, broad=True):
            continue
        loc = ", ".join(str(x) for x in (d.get("location"), d.get("country")) if x and str(x).lower() not in ("none", "null", "n/a"))
        blob = json.dumps(d)
        remote = True if RE_REMOTE.search(loc + " " + title + " " + str(d.get("remote") or d.get("work_arrangement") or d.get("workplace_type") or "")) else None
        if not loc_ok([loc], remote):
            continue
        desc = text_of(str(d.get("job_description") or ""))
        more = describe(url, title) if url and "slack.com" not in url else ""
        desc = more if len(more) > len(desc) else desc
        sal = next((str(v) for k, v in d.items() if "salary" in k.lower() or "compensation" in k.lower() if v and "$" in str(v)), "")
        out.append(dcand(id=rid, source="htn", board="Health Tech Nerds", company=co, title=title, locations=[loc] + (["Remote"] if remote and not RE_REMOTE.search(loc) else []),
                         url=url, posted="", pay=[sal] if sal else pay_snips(desc + " " + blob), eligibility=elig_snips(desc), excerpt=excerpt(desc), canon=canon_id(url)))
    note_ids("htn", ids)
    return out


def remoteboards():
    """Remote-job feeds with keyword search: Himalayas, Remotive, Jobicy."""
    out, ids, n = [], [], 0
    seen = {}
    for q in D["remoteQueries"]:
        for pg in (1, 2):
            try:
                j = jget(f"https://himalayas.app/jobs/api/search?q={urllib.parse.quote(q)}&country=US&page={pg}", tries=2)
            except Exception:
                break
            got = j.get("jobs") or []
            for r in got:
                seen[r.get("guid") or r.get("applicationLink")] = r
            time.sleep(0.5)
            if len(got) < 20:
                break
    n += len(seen)
    for guid, r in seen.items():
        rid = "hm-" + re.sub(r"[^a-z0-9-]+", "-", guid.split("/companies/")[-1].replace("/jobs/", "--").lower())[:110]
        ids.append(rid)
        title, co = r.get("title") or "", r.get("companyName") or ""
        desc = text_of(r.get("description") or r.get("excerpt") or "")
        if not title_ok(title, broad=True) or not healthy(co, title, desc):
            continue
        lr = r.get("locationRestrictions") or []
        if lr and not RE_USA.search(" ; ".join(lr)) and "United States" not in lr:
            continue
        pay = [f"${r['minSalary']:,.0f} - ${r['maxSalary']:,.0f} ({r.get('salaryPeriod') or 'annual'}, from Himalayas)"] if r.get("minSalary") and r.get("maxSalary") else pay_snips(desc)
        out.append(dcand(id=rid, source="himalayas", board="Himalayas", company=co, title=title, locations=["Remote"] + lr[:4], url=guid, posted=norm_date(r.get("pubDate")),
                         pay=pay, eligibility=elig_snips(desc), excerpt=excerpt(desc)))
    note_ids("hm", ids)
    try:
        rows = jget("https://remotive.com/api/remote-jobs?search=healthcare", tries=2).get("jobs") or []
        n += len(rows)
        note_ids("rm", [f"rm-{r['id']}" for r in rows])
        for r in rows:
            desc = text_of(r.get("description"))
            loc = r.get("candidate_required_location") or ""
            if title_ok(r.get("title"), broad=True) and healthy(r.get("company_name") or "", r.get("title") or "", desc) and (not loc or RE_USA.search(loc) or re.search(r"worldwide|americas", loc, re.I)):
                out.append(dcand(id=f"rm-{r['id']}", source="remotive", board="Remotive", company=r.get("company_name") or "", title=r["title"], locations=["Remote", loc],
                                 url=r.get("url"), posted=norm_date(r.get("publication_date")), pay=[r["salary"]] if "$" in (r.get("salary") or "") else pay_snips(desc),
                                 eligibility=elig_snips(desc), excerpt=excerpt(desc)))
    except Exception as e:
        COUNTS["remoteboards:remotive_error"] = str(e)[:80]
    try:
        rows = []
        for qs in ("tag=healthcare", "industry=data-science"):
            rows += jget(f"https://jobicy.com/api/v2/remote-jobs?count=50&geo=usa&{qs}", tries=2).get("jobs") or []
        n += len(rows)
        note_ids("jy", [f"jy-{r['id']}" for r in rows])
        for r in rows:
            desc = text_of(r.get("jobDescription") or r.get("jobExcerpt"))
            if title_ok(r.get("jobTitle"), broad=True) and healthy(r.get("companyName") or "", r.get("jobTitle", ""), desc):
                pay = [f"${r['salaryMin']:,.0f} - ${r['salaryMax']:,.0f} (from Jobicy)"] if r.get("salaryMin") and r.get("salaryMax") else pay_snips(desc)
                out.append(dcand(id=f"jy-{r['id']}", source="jobicy", board="Jobicy", company=html.unescape(r.get("companyName") or ""), title=html.unescape(r["jobTitle"]),
                                 locations=["Remote", r.get("jobGeo") or ""], url=r.get("url"), posted=norm_date(r.get("pubDate")), pay=pay, eligibility=elig_snips(desc), excerpt=excerpt(desc)))
    except Exception as e:
        COUNTS["remoteboards:jobicy_error"] = str(e)[:80]
    COUNTS["remoteboards:all"] = n
    return out


def adzuna():
    """Adzuna aggregator (needs ADZUNA_APP_ID / ADZUNA_APP_KEY repository secrets)."""
    aid, key = os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"]
    rows = {}
    for q in D["adzunaQueries"]:
        for extra in ("&where=Colorado", "&what_and=remote"):
            u = (f"https://api.adzuna.com/v1/api/jobs/us/search/1?app_id={aid}&app_key={key}&results_per_page=50&max_days_old=21"
                 f"&what={urllib.parse.quote(q)}{extra}&content-type=application/json")
            for r in jget(u, tries=2).get("results") or []:
                r["_remote"] = "what_and" in extra
                rows[str(r["id"])] = r
            time.sleep(1.2)
    note_ids("az", [f"az-{k}" for k in rows])
    COUNTS["adzuna:all"] = len(rows)
    out = []
    for k, r in rows.items():
        title = re.sub(r"</?strong>", "", r.get("title") or "")
        co = (r.get("company") or {}).get("display_name") or ""
        desc = text_of(re.sub(r"</?strong>", "", r.get("description") or ""))
        loc = (r.get("location") or {}).get("display_name") or ""
        if not title_ok(title, broad=True) or not healthy(co, title, desc + " " + desc):
            continue
        remote = True if (r["_remote"] and RE_REMOTE.search(title + " " + desc + " " + loc)) else None
        if not loc_ok([loc], remote):
            continue
        pay = []
        if r.get("salary_min") and r.get("salary_max") and str(r.get("salary_is_predicted")) != "1":
            pay = [f"${r['salary_min']:,.0f} - ${r['salary_max']:,.0f} (from Adzuna)"]
        out.append(dcand(id=f"az-{k}", source="adzuna", board="Adzuna", company=co, title=title, locations=[loc] + (["(mentions remote)"] if remote else []),
                         url=r.get("redirect_url"), posted=norm_date(r.get("created")), pay=pay or pay_snips(desc), eligibility=[], excerpt=excerpt(desc)))
    return out


def usajobs():
    """USAJobs (needs USAJOBS_KEY and USAJOBS_EMAIL repository secrets)."""
    hd = {"Host": "data.usajobs.gov", "User-Agent": os.environ["USAJOBS_EMAIL"], "Authorization-Key": os.environ["USAJOBS_KEY"]}
    rows = {}
    for q in D["usajobsQueries"]:
        for extra in ("&RemoteIndicator=True", "&LocationName=Denver,%20Colorado&Radius=40"):
            j = jget(f"https://data.usajobs.gov/api/search?Keyword={urllib.parse.quote(q)}&ResultsPerPage=100{extra}", headers=hd, tries=2)
            for it in ((j.get("SearchResult") or {}).get("SearchResultItems") or []):
                rows[str(it.get("MatchedObjectId"))] = (it.get("MatchedObjectDescriptor") or {}, "Remote" in extra)
    note_ids("uj", [f"uj-{k}" for k in rows])
    COUNTS["usajobs:all"] = len(rows)
    out = []
    for k, (r, rem) in rows.items():
        title = r.get("PositionTitle") or ""
        if not title_ok(title):
            continue
        det = ((r.get("UserArea") or {}).get("Details") or {})
        desc = text_of((det.get("JobSummary") or "") + " " + " ".join(det.get("MajorDuties") or []))
        pr = (r.get("PositionRemuneration") or [{}])[0]
        pay = [f"${float(pr['MinimumRange']):,.0f} - ${float(pr['MaximumRange']):,.0f} {pr.get('Description') or ''}"] if pr.get("MinimumRange") else []
        loc = r.get("PositionLocationDisplay") or ""
        out.append(dcand(id=f"uj-{k}", source="usajobs", board="USAJobs", company=f"{r.get('OrganizationName') or ''} ({r.get('DepartmentName') or ''})", title=title,
                         locations=[loc] + (["Remote"] if rem else []), url=r.get("PositionURI"), posted=norm_date(r.get("PublicationStartDate")), pay=pay,
                         eligibility=[], excerpt=excerpt(desc)))
    return out


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
    FULL = {"gh": "greenhouse", "lv": "lever", "ab": "ashby", "wk": "workable", "sr": "smartrecruiters",
            "bb": "bamboohr", "rp": "rippling", "ic": "icims", "pl": "paylocity", "adp": "adp", "ukg": "ukg",
            "bz": "breezy", "jb": "jibe"}
    if fam in FULL:
        # full board lists: absent means closed, unless that family had a failed board
        bad = [k for k in ERRORS if k.split(":")[0] == FULL[fam]]
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
    if fam == "or":
        _, pfx, rid = did.split("-", 2)
        c = next((c for c in CFG.get("oracle", []) if c["prefix"] == pfx), None)
        if not c:
            return "unknown"
        fd = urllib.parse.quote(f'ById;Id="{rid}",siteNumber={c["site"]}')
        st, _, body = check_url(f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?onlyData=true&finder={fd}")
        if st != 200:
            return "unknown"
        return "open" if '"Id"' in body else "closed"
    st, final, body = check_url(url)
    if st in (404, 410):
        return "closed"
    if st != 200:
        return "unknown"
    if fam in DISC_FAMS:
        # found by search, so absence from today's results proves nothing; trust the page itself
        if re.search(r"not a robot|just a moment", body[:3000], re.I):
            return "unknown"
        if re.search(r"no longer (available|accepting|open|active)|position has been filled|job (is )?not found|has expired|is closed|been removed", body, re.I):
            return "closed"
        return "open"
    key = did.rsplit("-", 1)[1] if fam in ("tb", "sf", "gj") else did.split("-", 1)[1]
    if key not in final and key not in body:
        return "closed"   # redirected to a search page
    if re.search(r"no longer (available|accepting|open)|position has been filled|job (is )?not found|this job has expired|job posting has expired|posting is no longer|page you (requested|are looking for) (could not|cannot|can.t) be found", body, re.I):
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
    tasks += [(f"talentbrew:{t['prefix']}", lambda t=t: talentbrew(t)) for t in CFG.get("talentbrew", [])]
    tasks += [(f"smartrecruiters:{c['slug']}", lambda c=c: smartrecruiters(c)) for c in CFG.get("smartrecruiters", [])]
    tasks += [(f"bamboohr:{c['slug']}", lambda c=c: bamboohr(c)) for c in CFG.get("bamboohr", [])]
    tasks += [(f"rippling:{c['slug']}", lambda c=c: rippling(c)) for c in CFG.get("rippling", [])]
    tasks += [(f"icims:{c['slug']}", lambda c=c: icims(c)) for c in CFG.get("icims", [])]
    for fam, fn, key in (("paylocity", paylocity, "prefix"), ("adp", adp, "prefix"), ("ukg", ukg, "prefix"), ("oracle", oracle, "prefix"),
                         ("breezy", breezy, "slug"), ("govjobs", govjobs, "agency"), ("successfactors", successfactors, "prefix"), ("jibe", jibe, "prefix")):
        tasks += [(f"{fam}:{c[key]}", lambda c=c, fn=fn: fn(c)) for c in CFG.get(fam, [])]
    tasks += [(f"getro:{g['host']}", lambda g=g: getro(g)) for g in CFG.get("getro", [])]
    tasks += [(f"naylor:{c['host']}", lambda c=c: naylor(c)) for c in CFG.get("naylor", [])]
    tasks += [(f"madgex:{c['prefix']}", lambda c=c: madgex(c)) for c in CFG.get("madgex", [])]
    tasks += [(f"webscribble:{c['prefix']}", lambda c=c: webscribble(c)) for c in CFG.get("webscribble", [])]
    tasks += [("hudson", hudson), ("htn", htn), ("remoteboards", remoteboards)]
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        tasks.append(("adzuna", adzuna))
    if os.environ.get("USAJOBS_KEY") and os.environ.get("USAJOBS_EMAIL"):
        tasks.append(("usajobs", usajobs))
    tasks += [("uhg", uhg), ("zelis", zelis), ("amazon", amazon), ("google", google), ("microsoft", microsoft)]
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(run_source, k, fn): k for k, fn in tasks}
        for f in cf.as_completed(futs):
            jobs.extend(f.result())
    nk = lambda j: (re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower())[:14], re.sub(r"[^a-z0-9]", "", (j.get("title") or "").lower()))
    uniq, keys = {}, set()
    for j in sorted(jobs, key=lambda j: bool(j.get("discovery"))):     # direct sources first
        if j.get("discovery"):
            # drop a discovery hit when a direct source (or an earlier board) already has the same job
            if j.get("canon") in uniq or nk(j) in keys:
                COUNTS["discovery:duplicates"] = COUNTS.get("discovery:duplicates", 0) + 1
                continue
        if j["id"] in uniq:
            continue
        j.pop("canon", None)
        uniq[j["id"]] = j
        keys.add(nk(j))
    cands = sorted(uniq.values(), key=lambda j: (j["source"], j.get("company", ""), j["title"]))
    COUNTS["discovery:candidates"] = sum(1 for j in cands if j.get("discovery"))
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(cands, open(os.path.join(OUT, "candidates.json"), "w"), indent=1)
    json.dump({k: sorted(v) for k, v in sorted(ALL_IDS.items())}, open(os.path.join(OUT, "all_ids.json"), "w"))
    verify = verify_all(cands, now[:10])
    json.dump({"checkedAt": now, "status": verify}, open(os.path.join(OUT, "verify.json"), "w"), indent=1)
    run = {"fetchedAt": now, "sources": len(tasks), "sourcesFailed": len(ERRORS), "candidates": len(cands),
           "errors": ERRORS, "counts": dict(sorted(COUNTS.items()))}
    json.dump(run, open(os.path.join(OUT, "run.json"), "w"), indent=1)
    json.dump(sorted(NEAR, key=lambda n: (n["why"], n["company"], n["title"] or "")), open(os.path.join(OUT, "near_misses.json"), "w"), indent=0)
    print(f"{now}: {len(tasks)} sources, {len(ERRORS)} failed, {len(cands)} candidates")
    for k, v in ERRORS.items():
        print(f"  FAILED {k}: {v}")
    # Fail the job only if most sources failed (likely a network problem)
    return 1 if len(ERRORS) > len(tasks) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
