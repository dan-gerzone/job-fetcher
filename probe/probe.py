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

VC = """jobs.a16z.com jobs.generalcatalyst.com jobs.oakhcft.com jobs.townhallventures.com jobs.7wireventures.com careers.flarecapital.com jobs.definevc.com jobs.define.vc jobs.venrock.com jobs.gv.com jobs.khoslaventures.com jobs.fprimecapital.com jobs.transformcap.com jobs.bvp.com careers.nea.com jobs.8vc.com jobs.redesignhealth.com jobs.rockhealth.com jobs.lrvhealth.com jobs.fcacapital.com jobs.optumventures.com jobs.echohealthventures.com jobs.406ventures.com jobs.polarispartners.com jobs.alleycorp.com jobs.thrivecap.com jobs.sequoiacap.com jobs.kleinerperkins.com jobs.lux.vc jobs.firstround.com jobs.foundersfund.com jobs.insightpartners.com jobs.accel.com jobs.greylock.com jobs.healthvelocitycapital.com jobs.frazierhealthcare.com jobs.maverickventures.com jobs.cignaventures.com jobs.blueventurefund.com jobs.healthworx.com jobs.providenceventures.com jobs.upmcenterprises.com jobs.questapartners.com jobs.sofinnovapartners.com jobs.oxeon.com jobs.wellingtonpartners.com jobs.norwest.com jobs.ga.com jobs.sparkcapital.com jobs.indexventures.com jobs.techstars.com jobs.foundrygroup.com jobs.boulder.vc careers.matchstick.vc jobs.rangeventures.com jobs.yearone.com jobs.coloradothrives.com jobs.builtincolorado.com jobs.digitalhealth.vc jobs.healthtech.nyc jobs.digitalhealthnyc.com jobs.hlth.com jobs.outofpocket.health jobs.healthtechnerds.com talent.healthtechnerds.com jobs.chartis.com jobs.bessemer.com jobs.arch.vc jobs.ensemble.vc jobs.casdin.com jobs.deerfield.com jobs.aehealth.com jobs.hcvp.com jobs.sverica.com jobs.newenterprise.com jobs.cotacapital.com jobs.kp.org jobs.generalatlantic.com jobs.wcas.com jobs.b-capital.com jobs.iaventures.com jobs.inovia.vc jobs.signalfire.com jobs.ycombinator.com""".split()

def vc(host):
    st, fin, b = get("https://" + host + "/jobs")
    if st != 200:
        st, fin, b = get("https://" + host + "/")
    r = {"st": st, "fin": fin[:80], "len": len(b)}
    if st != 200 or len(b) < 500:
        return r
    low = b.lower()
    r["getro"] = "getro" in low
    r["consider"] = "consider" in low and ("api-boards" in low or "consider.com" in low)
    m = re.search(r'"network"\s*:\s*\{[^{}]*?"id"\s*:\s*"?(\d+)', b) or re.search(r'"networkId"\s*:\s*"?(\d+)', b) or re.search(r'collections/(\d+)', b)
    r["netid"] = m.group(1) if m else None
    r["title"] = sq((re.search(r"<title[^>]*>(.*?)</title>", b, re.S) or [0, ""])[1], 70)
    m = re.search(r'"board"\s*:\s*\{[^{}]*?"id"\s*:\s*"([^"]+)"', b) or re.search(r'boardId["\']?\s*[:=]\s*["\']([^"\']+)', b)
    r["boardid"] = m.group(1) if m else None
    if r["netid"]:
        st2, _, b2 = get(f"https://api.getro.com/api/v2/collections/{r['netid']}/search/jobs", data={"hitsPerPage": 2, "page": 0, "filters": {"page": 0}, "query": "director"},
                         headers={"Origin": "https://" + host, "Referer": "https://" + host + "/jobs"})
        r["getro_api"] = [st2, sq(b2, 1500 if host == "jobs.oakhcft.com" else 160)]
    if r["consider"] or r["boardid"]:
        st2, _, b2 = get(f"https://{host}/api-boards/search-jobs", data={"meta": {"size": 2}, "board": {"id": r["boardid"] or host.split(".")[1], "isParent": True}, "query": {"promoteFeatured": False}},
                         headers={"Origin": "https://" + host, "Referer": "https://" + host + "/jobs"})
        r["consider_api"] = [st2, sq(b2, 1500 if host == "jobs.a16z.com" else 160)]
    return r

ASSOC = """jobs.himss.org jobmine.himss.org careers.academyhealth.org jobs.academyhealth.org careers.ache.org jobs.ache.org jobs.amia.org careers.amia.org jobs.hfma.org careers.hfma.org careers.nahq.org jobs.nahq.org careerassist.ahima.org jobs.ahima.org careers.apha.org jobs.apha.org careers.naccho.org careers.ahip.org jobs.ahip.org jobs.mgma.com careers.mgma.com careers.ispor.org jobs.ispor.org careers.amga.org jobs.amga.org careers.aapl.org jobs.physicianleaders.org careers.nachc.org jobs.healthcareercenter.com careers.aha.org careers.aamc.org jobs.aamc.org careers.nashp.org careers.asaecenter.org jobs.chimecentral.org careers.chimecentral.org jobs.healthcarefinancejobs.com careers.pqaalliance.org careers.ncqa.org jobs.healthecareers.com www.healthecareers.com careers.amstat.org jobs.informs.org careers.informs.org jobs.tableau.com careers.astho.org jobs.publichealthjobs.org www.publichealthjobs.org careers.aptrweb.org jobs.phf.org careers.cste.org jobs.coloradononprofits.org careers.coloradononprofits.org www.andrewhudsonsjobslist.com careers.acmanet.org jobs.caqh.org careers.hcca-info.org jobs.aapc.com careers.acdis.org jobs.healthleadersmedia.com careers.shsmd.org careers.acmpe.org jobs.modernhealthcare.com careers.beckershospitalreview.com jobs.fiercehealthcare.com careers.ahcancal.org careers.leadingage.org careers.npaonline.org jobs.npaonline.org careers.medicaiddirectors.org careers.communityplans.net""".split()

def assoc(host):
    out = {}
    for path in ("/jobs/?keywords=director", "/jobs?keywords=director", "/"):
        st, fin, b = get("https://" + host + path, timeout=15)
        if st == 200 and len(b) > 1500:
            low = b.lower()
            plat = [p for p, k in (("ymcareers", "ymcareers"), ("yourmembership", "yourmembership"), ("naylor", "naylor"), ("boxwood", "boxwood"), ("webscribble", "webscribble"), ("web scribble", "web scribble"), ("jobtarget", "jobtarget"), ("madgex", "madgex"), ("careerwebsite", "careerwebsite"), ("smartjobboard", "smartjobboard"), ("jobboard.io", "jobboard.io"), ("communitybrands", "communitybrands"), ("associationcareernetwork", "associationcareernetwork"), ("niceboard", "niceboard"), ("jboard", "jboard")) if k in low]
            links = re.findall(r'href="([^"]*?/jobs?/[^"]{6,120})"', b)
            i = b.find(links[3]) if len(links) > 3 else -1
            out = {"st": st, "fin": fin[:90], "len": len(b), "plat": plat, "nlinks": len(links), "links": links[2:6],
                   "title": sq((re.search(r"<title[^>]*>(.*?)</title>", b, re.S) or [0, ""])[1], 60),
                   "rss": re.findall(r'href="([^"]*(?:rss|feed)[^"]*)"', b, re.I)[:3],
                   "chunk": sq(b[max(0, i - 300): i + 900], 900) if i >= 0 and host in ("careers.academyhealth.org", "jobs.himss.org", "jobs.hfma.org", "careers.ache.org", "jobs.amia.org", "careers.nahq.org", "careers.apha.org") else ""}
            if links:
                break
        else:
            out = out or {"st": st, "fin": fin[:70], "len": len(b)}
    return out

def special():
    o = {}
    def rec(name, url, n=900, **kw):
        st, fin, b = get(url, **kw); o[name] = [st, len(b), sq(b, n)]
    rec("remotive", "https://remotive.com/api/remote-jobs?search=healthcare%20director&limit=3")
    rec("himalayas", "https://himalayas.app/jobs/api?limit=3")
    rec("himalayas_search", "https://himalayas.app/jobs/api/search?q=healthcare%20analytics%20director&limit=3")
    rec("remoteok", "https://remoteok.com/api?tag=healthcare", n=700)
    rec("jobicy", "https://jobicy.com/api/v2/remote-jobs?count=3&geo=usa&tag=healthcare")
    rec("themuse", "https://www.themuse.com/api/public/jobs?category=Data%20and%20Analytics&level=Senior%20Level&level=management&location=Flexible%20%2F%20Remote&page=1", n=1100)
    rec("wwr_rss", "https://weworkremotely.com/remote-jobs.rss", n=500)
    rec("workingnomads", "https://www.workingnomads.com/api/exposed_jobs/", n=500)
    rec("usajobs_nokey", "https://data.usajobs.gov/api/search?Keyword=health%20data%20analytics%20director&ResultsPerPage=2", n=300)
    rec("adzuna_nokey", "https://api.adzuna.com/v1/api/jobs/us/search/1?what=director%20healthcare%20analytics&where=denver", n=300)
    rec("ycombinator", "https://www.workatastartup.com/companies?industry=Healthcare", n=300)
    rec("builtin_co", "https://www.builtincolorado.com/jobs?search=director+analytics+healthcare", n=300)
    rec("wittkieffer", "https://www.wittkieffer.com/positions", n=500)
    rec("getro_search_health", "https://api.getro.com/api/v2/collections/1/search/jobs", n=200, data={"hitsPerPage": 1, "page": 0, "filters": {}, "query": ""})
    return o

out = {"special": special(), "vc": {}, "assoc": {}}
with cf.ThreadPoolExecutor(24) as ex:
    for h, r in zip(VC, ex.map(vc, VC)): out["vc"][h] = r
    for h, r in zip(ASSOC, ex.map(assoc, ASSOC)): out["assoc"][h] = r
json.dump(out, open(os.path.join(HERE, "out.json"), "w"), indent=0)
