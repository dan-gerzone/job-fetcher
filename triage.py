#!/usr/bin/env python3
"""Compare the fetcher's output with the tracker so Claude only reads what needs judgment.

Run in the Claude routine's cloud workspace after downloading data/ from GitHub and
exporting the tracker's "jobs" collection with ArtifactData (out_dir).

  triage.py summary  --data D --tracker T --reviewed R.json   # what's new / closed / changed
  triage.py show ID [ID ...] --data D                         # full detail for chosen candidates
  triage.py tracked --tracker T > tracked.json                # open rows for the fetcher to re-check
"""
import argparse
import glob
import json
import os
import re
import sys

OPEN = ("new", "interested")


def load_tracker(d):
    rows = {}
    for f in glob.glob(os.path.join(d, "**", "*.json"), recursive=True):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        doc = j.get("data", j)
        did = j.get("id") or os.path.splitext(os.path.basename(f))[0]
        rows[did] = doc
    return rows


def nums(s):
    """Dollar figures in a pay string, normalized to whole dollars."""
    out = set()
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?", s or ""):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        if v >= 20000:
            out.add(int(round(v / 1000.0)))
    return out


def short(c, n=150):
    pay = "; ".join(c.get("pay") or [])[:n] or "-"
    locs = "; ".join(c.get("locations") or [])[:110]
    return f"{c['id']} | {c.get('company')} | {c['title']} | {locs} | posted {c.get('posted') or '?'} | pay: {pay}"


def cmd_summary(a):
    data = a.data
    cands = {c["id"]: c for c in json.load(open(os.path.join(data, "candidates.json")))}
    run = json.load(open(os.path.join(data, "run.json")))
    vpath = os.path.join(data, "verify.json")
    verify = json.load(open(vpath)).get("status", {}) if os.path.exists(vpath) else {}
    tracker = load_tracker(a.tracker)
    reviewed = set(json.load(open(a.reviewed))) if a.reviewed and os.path.exists(a.reviewed) else set()

    print(f"FETCHED {run['fetchedAt']} | sources {run['sources']} | failed {run['sourcesFailed']} | candidates {run['candidates']}")
    for k, v in (run.get("errors") or {}).items():
        print(f"  source failed: {k}: {v[:140]}")

    review = [c for i, c in cands.items() if i not in tracker and i not in reviewed]
    print(f"\nTO REVIEW ({len(review)}) - not in tracker and not reviewed before:")
    for c in review:
        print("  " + short(c))

    closes, unknown = [], []
    for i, d in tracker.items():
        if d.get("status") not in OPEN:
            continue
        st = verify.get(i)
        if i in cands:
            continue
        if st == "closed":
            closes.append(i)
        elif st != "open":
            unknown.append(f"{i} ({st or 'not checked'})")
    print(f"\nCLOSE ({len(closes)}): " + ", ".join(closes))
    if unknown:
        print("COULD NOT VERIFY (leave as is): " + ", ".join(unknown))

    print("\nPOSSIBLE PAY/LOCATION CHANGES (tracked rows still listed):")
    n = 0
    for i, d in tracker.items():
        c = cands.get(i)
        if not c or d.get("status") == "closed":
            continue
        tp, cp = nums(d.get("pay")), nums(" ".join(c.get("pay") or []))
        if tp and cp and not (min(tp) in cp and max(tp) in cp):
            n += 1
            print(f"  {i} | tracker pay: {d.get('pay')} | posting now: {'; '.join(c['pay'])[:200]}")
    if not n:
        print("  none")
    print(f"\nREVIEWED_IDS_TO_ADD: {json.dumps([c['id'] for c in review])}")


def cmd_show(a):
    cands = {c["id"]: c for c in json.load(open(os.path.join(a.data, "candidates.json")))}
    for i in a.ids:
        c = cands.get(i)
        if not c:
            print(f"== {i}: not found\n")
            continue
        print(f"== {i}\n{c.get('company')} | {c['title']}\nurl: {c.get('url')}\nposted: {c.get('posted')}"
              f"\nlocations: {'; '.join(c.get('locations') or [])[:600]}\nworkplace: {c.get('workplace', '')}"
              f"\npay: {' || '.join(c.get('pay') or []) or '(none found)'}"
              f"\neligibility: {' || '.join(c.get('eligibility') or []) or '-'}"
              f"\nexcerpt: {(c.get('excerpt') or '')[:a.chars]}\n")


def cmd_tracked(a):
    tracker = load_tracker(a.tracker)
    out = [{"id": i, "url": d.get("url", "")} for i, d in sorted(tracker.items()) if d.get("status") in OPEN]
    json.dump(out, sys.stdout, indent=1)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("--data", required=True)
    s.add_argument("--tracker", required=True)
    s.add_argument("--reviewed")
    s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("show")
    s.add_argument("ids", nargs="+")
    s.add_argument("--data", required=True)
    s.add_argument("--chars", type=int, default=1400)
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("tracked")
    s.add_argument("--tracker", required=True)
    s.set_defaults(fn=cmd_tracked)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
