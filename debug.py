#!/usr/bin/env python3
"""Temporary diagnostics. Writes debug/out.txt."""
import json, os, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch as F
OUT = []
host = "https://apply.careers.microsoft.com"
j = F.jget(f"{host}/api/pcsx/search?domain=microsoft.com&query=director&location=United%20States&start=0&filter_include_remote=1")
ps = (j.get("data") or {}).get("positions") or []
OUT.append("national director: " + json.dumps([(p.get("name"), p.get("locations", [])[:2], p.get("workLocationOption")) for p in ps])[:1500])
if ps:
    k = ps[0]["id"]
    d = F.jget(f"{host}/api/pcsx/position_details?position_id={k}&domain=microsoft.com&hl=en")
    dd = d.get("data") or {}
    OUT.append("details top keys: " + str(list(d.keys())) + " data keys: " + str(list(dd.keys()) if isinstance(dd, dict) else type(dd)))
    for key, v in (dd.items() if isinstance(dd, dict) else []):
        OUT.append(f"  {key}: {str(v)[:200]!r}")
os.makedirs("debug", exist_ok=True)
open("debug/out.txt", "w").write("\n".join(OUT))
