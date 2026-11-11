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
    def rec(name, url, n=1400, find=None, **kw):
        st, fin, b = get(url, timeout=25, **kw)
        i = b.find(find) if find else 0
        out[name] = [st, fin[:200], len(b), re.sub(r"\s+", " ", b[max(0, i - 100):])[:n]]
        return b
    for nm, g in (("ncqa", "e17d268e-1abe-4e11-8fa8-bc9215bce4c0"), ("mgma", "deaaa457-a0ad-40a9-947c-5e028d563548")):
        rec(f"paylocity_{nm}", f"https://recruiting.paylocity.com/recruiting/jobs/All/{g}/x", find="window.pageData")
    for nm, cid in (("civhc", "d203d09b-4ff6-4f17-b8bb-e46f24573365"), ("pcori", "8eb0f259-f0ed-4366-8525-92ace3cd3304")):
        rec(f"adp_{nm}", f"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&timeStamp=1&lang=en_US&ccId=19000101_000001&locale=en_US&$top=50")
        rec(f"adp_{nm}_noccid", f"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&lang=en_US&locale=en_US&$top=50", n=500)
    st, fin, b = get("https://www.chcs.org/careers"); out["chcs_final"] = fin
    st, fin, b = get("https://www.cdcfoundation.org/jobs"); out["cdcf_adp"] = re.findall(r"workforcenow\.adp\.com[^\"' ]+", b)[:3]
    for nm, u in (("milliman", "https://recruiting2.ultipro.com/MIL1017/JobBoard/f54234e9-dfde-b183-fd20-4fbdb19cba7a"), ("ddco", "https://recruiting2.ultipro.com/ENS1001ENINV/JobBoard/6c6864f0-3e3c-4843-a21f-cd2ef5956a81")):
        rec(f"ukg_{nm}", u + "/JobBoardView/LoadSearchResults", data={"opportunitySearch": {"Top": 3, "Skip": 0, "QueryString": "", "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}], "Filters": []}, "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}, n=2200)
    for nm, host, site in (("bsca", "ecge.fa.us2.oraclecloud.com", "CX_1003"), ("bcbsm", "ejko.fa.us2.oraclecloud.com", "CX_3"), ("guidewell", "fa-etum-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1"), ("abt", "egpy.fa.us2.oraclecloud.com", "JoinAbt"), ("mayo", "fa-euwp-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1")):
        rec(f"oracle_{nm}", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},facetsList=NONE,limit=3,keyword=director,sortBy=POSTING_DATES_DESC", n=1800 if nm == "bsca" else 500)
    b = rec("oracle_bsca_detail_hint", "https://ecge.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%221%22,siteNumber=CX_1003", n=300)
    rec("breezy_sequoia", "https://the-sequoia-project.breezy.hr/json", n=900)
    rec("govjobs_co", "https://www.governmentjobs.com/careers/home/index?agency=colorado&keyword=director&sort=PostingDate&isDescendingSort=true", n=2600, find='<li class="list-item', headers={"X-Requested-With": "XMLHttpRequest"})
    rec("sf_gainwell", "https://jobs.gainwelltechnologies.com/search/?q=director&startrow=0", n=1800, find='class="jobTitle-link"')
    rec("sf_bsci", "https://jobs.bostonscientific.com/search/?q=director&startrow=0", n=600, find='class="jobTitle-link"')
    rec("tb_commonspirit", "https://www.commonspirit.careers/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=5&Keywords=director+analytics&SearchResultsModuleName=Search+Results&SearchFiltersModuleName=Search+Filters&SearchType=5&OrganizationIds=35300", n=1500, headers={"X-Requested-With": "XMLHttpRequest"})
    for nm, h in (("dispatch", "careers.dispatchhealth.com"), ("innovage", "careers.innovage.com"), ("rti", "careers.rti.org"), ("acentra", "careers.acentra.com"), ("uchealth", "careers.uchealth.org")):
        rec(f"jibe_{nm}", f"https://{h}/api/jobs?page=1&limit=3&keywords=director", n=1500 if nm == "rti" else 500, headers={"Referer": f"https://{h}/jobs"})
    rec("dayforce_coaccess", "https://jobs.dayforcehcm.com/api/geo/coaccess/jobposting/search", n=600, data={"clientNamespace": "coaccess", "jobBoardCode": "CANDIDATEPORTAL", "cultureCode": "en-US", "distanceUnit": 0, "paginationStart": 0})
    rec("dayforce_coaccess_page", "https://jobs.dayforcehcm.com/en-US/coaccess/CANDIDATEPORTAL", n=900, find="__NEXT_DATA__")
    rec("paycor_chf", "https://recruitingbypaycor.com/career/CareerHome.action?clientId=8aa005063388231001338fa23a750023", n=1500, find="gnewtonJobLink")
    rec("uchealth_page", "https://careers.uchealth.org/jobs?keywords=director", n=900, find="api")
    rec("eightfold_mayo", "https://jobs.mayoclinic.org/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=5&Keywords=director+analytics&SearchType=5", n=700, headers={"X-Requested-With": "XMLHttpRequest"})
    return out

slugs = open(os.path.join(HERE, "targets.txt")).read().split()
domains = open(os.path.join(HERE, "domains.txt")).read().split()
out = {"slug": {}, "sniff": {}, "special": {}}
try:
    out["special"] = special()
except Exception as e:
    out["special"] = {"error": str(e)}
json.dump(out, open(os.path.join(HERE, "out.json"), "w"), indent=0)
print(len(out["slug"]), "slug hits of", len(slugs))
