# Job fetcher

Pulls healthcare analytics leadership listings from Greenhouse, Lever, Ashby, Workable,
Workday (Elevance, Humana, CVS, Centene, Evolent, Health Catalyst), UnitedHealth Group,
Zelis, Amazon, Google and Microsoft, and writes a pre-filtered candidate list to `data/`.
A Claude routine reads `data/`, judges fit and updates the job tracker.

- **Runs:** daily at ~5:17am Denver, or on demand (Actions tab → "Fetch job listings" → Run workflow).
- **Add a company:** add its board slug to the right list in `config.json`
  (Greenhouse / Lever / Ashby), or a new entry under `workday`. No code changes needed.
- **Tune the title filter:** edit the regexes under `filters` in `config.json`.
- `tracked.json` is written by the Claude routine: open tracker rows the fetcher re-checks for closure.
- `data/run.json` shows per-source counts and any failures.
