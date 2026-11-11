# Job fetcher

Pulls healthcare analytics leadership listings from Greenhouse, Lever, Ashby, Workable,
Workday (Elevance, Humana, CVS, Centene, Evolent, Health Catalyst), UnitedHealth Group,
Zelis, Amazon, Google and Microsoft, and writes a pre-filtered candidate list to `data/`.
A Claude routine reads `data/`, judges fit and updates the job tracker.

- **Runs:** daily at 10:50am Denver, started by an outside scheduler (cron-job.org, using a fine-grained token
  limited to Actions on this repo; renew it yearly). GitHub's own runs at 07:17, 11:17 and 15:17 UTC are a backup; GitHub often
  starts those late or skips them, or on demand (Actions tab → "Fetch job listings" → Run workflow).
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
- **Not covered yet** (job boards the fetcher can't read, or that block GitHub's servers): UCHealth, Children's Hospital
  Colorado, Colorado Access (Dayforce), Colorado Health Foundation (Paycor), DispatchHealth, CAQH, Mathematica, Maximus
  (Avature), Westat, Conduent / MITRE / Zimmer Biomet / Guardant (Phenom), ECRI (Paycom), AHIMA, ASTHO, Vital Strategies.
