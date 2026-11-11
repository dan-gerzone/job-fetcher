# Job fetcher

Pulls healthcare analytics leadership listings from Greenhouse, Lever, Ashby, Workable,
Workday (Elevance, Humana, CVS, Centene, Evolent, Health Catalyst), UnitedHealth Group,
Zelis, Amazon, Google and Microsoft, and writes a pre-filtered candidate list to `data/`.
A Claude routine reads `data/`, judges fit and updates the job tracker.

- **Runs:** three times a day (07:17, 11:17, 15:17 UTC; GitHub often starts them late), or on demand (Actions tab → "Fetch job listings" → Run workflow).
- **Add a company:** add its board slug to the right list in `config.json`
  (Greenhouse / Lever / Ashby), or a new entry under `workday`. No code changes needed.
- **Tune the title filter:** edit the regexes under `filters` in `config.json`.
- Closures: `data/history.json` remembers every listing ever passed to Claude; each run re-checks the ones that
  disappeared and records them in `data/verify.json`. `tracked.json` is optional (extra ids to check).
- The Claude routine only reads this repo (it is public so no credentials are needed); it never pushes.
- `data/run.json` shows per-source counts and any failures.
- **Broad employers** (health systems, med tech, consultancies) are marked `"broad": true` (or listed in `broadBoards`):
  their titles must also match `filters.broadFunction`. `data/near_misses.json` lists senior titles the function or
  location screen dropped, so the filter can be audited.
- **Not covered yet** (job boards the fetcher can't read): UCHealth, Children's Hospital Colorado, CIVHC, Colorado Access,
  MGMA, NCQA (Paylocity), CAQH, Gainwell (SuccessFactors), Maximus (Avature), Mathematica (blocks GitHub), Milliman,
  Boston Scientific (Eightfold), Providence, Mayo Clinic.
