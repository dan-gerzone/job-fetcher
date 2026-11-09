#!/usr/bin/env python3
"""Temporary diagnostics. Writes debug/out.txt."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch as F
OUT = []
for w in F.CFG["workday"]:
    if w["prefix"] not in ("elv", "cen"):
        continue
    base = f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}"
    seen = []
    for off in range(0, 400, 20):
        jp = F.jget(f"{base}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": ""}).get("jobPostings") or []
        seen += jp
        if len(jp) < 20: break
    for x in seen:
        if not F.title_ok(x.get("title")): continue
        p = F.jget(base + x["externalPath"]).get("jobPostingInfo") or {}
        locs = [p.get("location", "")] + list(p.get("additionalLocations") or [])
        t = F.text_of(p.get("jobDescription"))
        m = re.search(r".{0,60}(virtual|remote|work from home).{0,80}", t[:4000], re.I)
        OUT.append(f"{w['prefix']} | {x['title'][:60]} | {'; '.join(locs)[:90]} | {m.group(0) if m else '-'}")
os.makedirs("debug", exist_ok=True)
open("debug/out.txt", "w").write("\n".join(OUT))
