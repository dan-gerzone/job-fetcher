# Job fetcher

Pulls healthcare analytics leadership listings from Greenhouse, Lever, Ashby, Workable,
Workday (Elevance, Humana, CVS, Centene, Evolent, Health Catalyst), UnitedHealth Group,
Zelis, Amazon, Google and Microsoft, and writes a pre-filtered candidate list to `data/`.
A Claude routine reads `data/`, judges fit and updates the job tracker.

- **Runs:** daily at ~5:17am Denver, or on demand (Actions tab → "Fetch job listings" → Run workflow).
- **Add a company:** add its board slug to the right list in `config.json`
  (Greenhouse / Lever / Ashby), or a new entry under `workday`. No code changes needed.
- **Tune the title filter:** edit the regexes under `filters` in `config.json`.
- Closures: `data/history.json` remembers every listing ever passed to Claude; each run re-checks the ones that
  disappeared and records them in `data/verify.json`. `tracked.json` is optional (extra ids to check).
- The Claude routine only reads this repo (it is public so no credentials are needed); it never pushes.
- `data/run.json` shows per-source counts and any failures.
