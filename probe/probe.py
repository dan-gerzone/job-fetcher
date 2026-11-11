#!/usr/bin/env python3
"""One-off discovery: which job-board system does each employer use? Writes probe/out.json."""
import concurrent.futures as cf, json, os, re, ssl, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CTX = ssl.create_default_context()
WDN = list(range(1, 13)) + list(range(100, 126)) + list(range(201, 206)) + list(range(501, 511))

def get(url, data=None, timeout=10, headers=None, _retry=True):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, */*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    if data is not None:
        data = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout, context=CTX) as r:
            return r.status, r.geturl(), r.read(30000000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:
        if _retry:
            return get(url, None if data is None else json.loads(data), timeout, headers, _retry=False)
        return 0, url, type(e).__name__

def J(b):
    try: return json.loads(b)
    except Exception: return None

def titles(xs, key): return [str(x.get(key, ""))[:45] for x in xs[:3]]

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
    if st == 200 and j and (j.get("totalFound") or 0) >= 3:
        c = (j["content"][0].get("company") or {}).get("name") if j.get("content") else None
        hits.append(["smartrecruiters", slug, j["totalFound"], c, titles(j.get("content") or [], "name")])
    st, _, b = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"); j = J(b)
    if st == 200 and j and j.get("jobs"):
        hits.append(["workable", slug, len(j["jobs"]), j.get("name"), titles(j["jobs"], "title")])
    st, _, b = get(f"https://{slug}.bamboohr.com/careers/list"); j = J(b)
    if st == 200 and j and j.get("result"):
        hits.append(["bamboohr", slug, len(j["result"]), None, titles(j["result"], "jobOpeningName")])
    st, _, b = get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"); j = J(b)
    if st == 200 and isinstance(j, list) and j:
        hits.append(["rippling", slug, len(j), None, titles(j, "name")])
    st, fin, b = get(f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1")
    n = len(set(re.findall(r"/jobs/(\d+)/", b))) if st == 200 else 0
    if n:
        hits.append(["icims", slug, n, None, [re.sub(r"\s+", " ", t)[:45] for t in re.findall(r'title="\s*\d+\s*-\s*([^"]+)"', b)[:3]]])
    for n in WDN:
        host = f"{slug}.wd{n}.myworkdayjobs.com"
        st, fin, b = get(f"https://{host}/robots.txt", timeout=7)
        if st == 200 and "sitemap" in b.lower():
            sites = sorted(set(re.findall(r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/\s]+)/siteMap", b, re.I)))
            res = []
            for s in sites[:40]:
                st2, _, b2 = get(f"https://{host}/wday/cxs/{slug}/{s}/jobs", data={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
                t = (J(b2) or {}).get("total")
                if t: res.append([s, t])
            hits.append(["workday", host, None, None, sorted(res, key=lambda r: -r[1])[:6]])
            break
    return hits

ATS = re.compile(r"https?://[A-Za-z0-9._/%?=&+-]*(?:myworkdayjobs\.com|icims\.com|greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|bamboohr\.com|rippling\.com|paylocity\.com|adp\.com|ultipro\.com|ukg\.com|dayforcehcm\.com|paycomonline\.net|paycor\.com|applytojob\.com|jobvite\.com|taleo\.net|oraclecloud\.com|successfactors\.(?:com|eu)|avature\.net|eightfold\.ai|phenompeople\.com|breezy\.hr|teamtailor\.com|recruitee\.com|pinpointhq\.com|clearcompany\.com|applicantpro\.com|isolvedhire\.com|hirebridge\.com|jazz\.co|jazzhr\.com|governmentjobs\.com|csod\.com|silkroad\.com|brassring\.com|trinethire\.com|gusto\.com|workforcenow|recruiting\.ultipro|careers-page\.com|jibeapply\.com|tal\.net|peopleadmin\.com|hrmdirect\.com|applicantstack\.com|paylocity)[A-Za-z0-9._/%?=&+#-]*", re.I)

def sniff(domain):
    found, pages = set(), []
    for path in ("/careers", "/about/careers", "/about-us/careers", "/careers/", "/jobs", "/about/work-with-us", "/company/careers", "/join-our-team", "/who-we-are/careers", ""):
        for pre in ("https://www.", "https://careers.", "https://jobs."):
            if pre != "https://www." and path:
                continue
            st, fin, b = get(pre + domain + path, timeout=10)
            if st == 200 and len(b) > 500:
                pages.append(f"{pre}{domain}{path} -> {fin}"[:140])
                for m in ATS.findall(b):
                    found.add(m[:150])
                if any(k in fin for k in ("icims", "workday", "paylocity", "ultipro", "taleo", "oraclecloud", "successfactors", "avature", "eightfold")):
                    found.add("REDIRECT " + fin[:150])
                # TalentBrew / Jibe / SuccessFactors-RMK fingerprints on self-hosted career sites
                if "tbcdn.talentbrew.com" in b or "/search-jobs" in b: found.add(f"TALENTBREW {fin[:80]} org={re.findall(r'data-org-id=.(\d+)', b)[:1] or re.findall(r'OrganizationIds?=(\d+)', b)[:1]}")
                if "jibe" in b.lower(): found.add(f"JIBE {fin[:100]}")
                if "rmkcdn.successfactors" in b or "jobs2web" in b: found.add(f"SFRMK {fin[:100]}")
                if "phenom" in b.lower(): found.add(f"PHENOM {fin[:100]}")
    return {"ats": sorted(found)[:14], "pages": pages[:4]}

def special():
    out = {}
    def rec(name, url, **kw):
        st, fin, b = get(url, timeout=20, **kw)
        out[name] = [st, fin[:120], re.sub(r"\s+", " ", b)[:700]]
        return b
    rec("jibe_uchealth", "https://careers.uchealth.org/api/jobs?limit=3&page=1&keywords=director")
    rec("eightfold_bsci", "https://bostonscientific.eightfold.ai/api/pcsx/search?domain=bostonscientific.com&query=director&location=United%20States&start=0")
    rec("eightfold_bsci_old", "https://bostonscientific.eightfold.ai/api/apply/v2/jobs?domain=bostonscientific.com&query=director&num=3")
    b = rec("sf_gainwell", "https://jobs.gainwelltechnologies.com/search/?q=director&startrow=0")
    out["sf_gainwell_rows"] = re.findall(r'<a[^>]+class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', b)[:4] + re.findall(r'class="jobLocation"[^>]*>\s*([^<]+)', b)[:4]
    b = rec("paylocity_ncqa_detail", "https://recruiting.paylocity.com/Recruiting/Jobs/Details/3807086")
    g = re.findall(r"/[Rr]ecruiting/[Jj]obs/All/([0-9a-fA-F-]{36})", b)
    out["paylocity_ncqa_guid"] = g[:2]
    if g:
        b2 = rec("paylocity_ncqa_list", f"https://recruiting.paylocity.com/recruiting/jobs/All/{g[0]}/x")
        m = re.search(r"window\.pageData\s*=\s*(\{.*?\});", b2, re.S)
        j = J(m.group(1)) if m else None
        out["paylocity_ncqa_jobs"] = [len((j or {}).get("Jobs") or []), json.dumps(((j or {}).get("Jobs") or [None])[0])[:700]]
    for s in ("caqh", "CAQH", "caqh-careers", "caqh-inc"):
        rec(f"rippling_{s}", f"https://api.rippling.com/platform/api/ats/v1/board/{s}/jobs")
    b = rec("rippling_caqh_page", "https://ats.rippling.com/caqh/jobs")
    out["rippling_caqh_hints"] = sorted(set(re.findall(r'"(?:boardSlug|slug|board)"\s*:\s*"([^"]+)"', b)))[:6] + re.findall(r"https://[a-z.]*rippling\.com/[A-Za-z0-9/_.-]*api[A-Za-z0-9/_.-]*", b)[:6]
    rec("tb_mathematica", "https://careers.mathematica.org/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=5&Keywords=director&SearchResultsModuleName=Search+Results&SearchFiltersModuleName=Search+Filters&SearchType=5&OrganizationIds=727",
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://careers.mathematica.org/search-jobs"})
    rec("govjobs_co", "https://www.governmentjobs.com/careers/home/index?agency=colorado&keyword=director&sort=PostingDate&isDescendingSort=true", headers={"X-Requested-With": "XMLHttpRequest"})
    rec("chco", "https://www.childrenscolorado.org/careers/job-search/?keyword=director")
    return out

slugs = open(os.path.join(HERE, "targets.txt")).read().split()
domains = open(os.path.join(HERE, "domains.txt")).read().split()
out = {"slug": {}, "sniff": {}, "special": {}}
try:
    out["special"] = special()
except Exception as e:
    out["special"] = {"error": str(e)}
with cf.ThreadPoolExecutor(40) as ex:
    for s, h in zip(slugs, ex.map(probe, slugs)):
        if h: out["slug"][s] = h
    for d, r in zip(domains, ex.map(sniff, domains)):
        out["sniff"][d] = r
json.dump(out, open(os.path.join(HERE, "out.json"), "w"), indent=0)
print(len(out["slug"]), "slug hits of", len(slugs))
