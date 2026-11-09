# Copy of the TRIAGE SCRIPT embedded in the Claude scheduled task prompt (the task never runs code from this repo).
import argparse, glob, json, os, re
def L(p): return json.load(open(p))
def ids_of(p):
    if not p or not os.path.exists(p): return set()
    r = L(p); r = r.get("data", r) if isinstance(r, dict) else r
    return set(r.get("ids", []) if isinstance(r, dict) else r)
def tracker(d):
    d = os.path.join(d, "jobs") if os.path.isdir(os.path.join(d, "jobs")) else d
    out = {}
    for f in glob.glob(os.path.join(d, "*.json")):
        j = L(f); out[j.get("id") or os.path.basename(f)[:-5]] = j.get("data", j)
    return out
def nums(s):
    o = set()
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?(?:\s*(?:-|–|—|to)\s*\$?\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?)?", s or ""):
        for g, k in ((m[1], m[2]), (m[3], m[4])):
            if g:
                v = float(g.replace(",", "")) * (1000 if k else 1)
                if v >= 20000: o.add(round(v / 1000))
    return o
p = argparse.ArgumentParser(); p.add_argument("cmd"); p.add_argument("ids", nargs="*")
p.add_argument("--data"); p.add_argument("--tracker"); p.add_argument("--reviewed"); p.add_argument("--merged-out")
a = p.parse_args(); D = a.data
C = {c["id"]: c for c in L(os.path.join(D, "candidates.json"))}
if a.cmd == "show":
    for i in a.ids:
        c = C.get(i)
        if not c: print(f"== {i}: not found\n"); continue
        print(f"== {i}\n{c.get('company')} | {c['title']}\nurl: {c.get('url')}\nposted: {c.get('posted')}\nlocations: {'; '.join(c.get('locations') or [])[:600]}\nworkplace: {c.get('workplace','')}\npay: {' || '.join(c.get('pay') or []) or '(none found)'}\neligibility: {' || '.join(c.get('eligibility') or []) or '-'}\nexcerpt: {(c.get('excerpt') or '')[:1400]}\n")
    raise SystemExit
R = L(os.path.join(D, "run.json")); vp = os.path.join(D, "verify.json")
V = L(vp).get("status", {}) if os.path.exists(vp) else {}
T = tracker(a.tracker); rev = ids_of(a.reviewed)
print(f"FETCHED {R['fetchedAt']} | sources {R['sources']} | failed {R['sourcesFailed']} | candidates {R['candidates']}")
for k, v in (R.get("errors") or {}).items(): print(f"  source failed: {k}: {v[:140]}")
new = [c for i, c in C.items() if i not in T and i not in rev]
print(f"\nTO REVIEW ({len(new)}) - not in tracker and not reviewed before:")
for c in new:
    print(f"  {c['id']} | {c.get('company')} | {c['title']} | {'; '.join(c.get('locations') or [])[:110]} | posted {c.get('posted') or '?'} | pay: {'; '.join(c.get('pay') or [])[:150] or '-'}")
cl, unk = [], []
for i, d in T.items():
    if d.get("status") not in ("new", "interested") or i in C: continue
    st = V.get(i)
    if st == "closed": cl.append(i)
    elif st != "open": unk.append(f"{i} ({st or 'not checked'})")
print(f"\nCLOSE ({len(cl)}): " + ", ".join(cl))
if unk: print("COULD NOT VERIFY (leave as is): " + ", ".join(unk))
print("\nPOSSIBLE PAY/LOCATION CHANGES (tracked rows still listed):"); n = 0
for i, d in T.items():
    c = C.get(i)
    if not c or d.get("status") == "closed": continue
    tp, cp = nums(d.get("pay")), nums(" ".join(c.get("pay") or []))
    if tp and cp and not (min(tp) in cp and max(tp) in cp):
        n += 1; print(f"  {i} | tracker pay: {d.get('pay')} | posting now: {'; '.join(c['pay'])[:200]}")
if not n: print("  none")
merged = sorted(rev | {c["id"] for c in new})
if a.merged_out:
    json.dump({"ids": merged}, open(a.merged_out, "w"))
    print(f"\nREVIEWED_MERGED_FILE: {a.merged_out} ({len(merged)} ids)")
