# Test Runbook — 8-Task Verification (ready to run)

Copy-paste commands to test the **actual working** of every feature. Each section
lists the command, the **YAML keys that control it**, and what a successful result
looks like. All writes target **test tabs only** (`Jobs_Test`, `CompaniesTest`).

- Run everything from the project root:
  `cd /home/skyflock/PX4-Autopilot/src/project/automated-scraping`
- Use the project venv: commands below use `.venv/bin/python` (or `source .venv/bin/activate` first).
- Logs for every run land in `logs/<name>_<timestamp>.log`.

---

## 0. Pre-flight

### 0a. Turn on verbose logging (recommended while testing)
**YAML** — `config.yaml`:
```yaml
logging:
  level: debug        # info | debug | warning | error   ← set to debug for testing
```

### 0b. Confirm Google Sheets connectivity + that the test tabs exist
**YAML pointers:** `google_sheets.enabled`, `google_sheets.credentials_file`,
`google_sheets.spreadsheet_id`
```bash
.venv/bin/python -c "
from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
cfg = load_config('config.yaml')
ss = GoogleSheetsStore(cfg['google_sheets'])._get_spreadsheet()
print('CONNECTED:', ss.title)
print('Tabs:', [w.title for w in ss.worksheets()])
"
```
**Expect:** `CONNECTED: LinkedIN Reachout` and a tab list including `Jobs_Test`,
`CompaniesTest`, `Company`, `Keywords`.

---

## Task #6 — Keywords from Google Sheets

**Command:**
```bash
.venv/bin/python -c "
from config_loader import load_config
from main import resolve_keywords
kws = resolve_keywords(load_config('config.yaml'))
print(len(kws), 'keywords from sheet:', kws)
"
```
**YAML pointers** — `config.yaml`:
```yaml
scraping:
  keywords_source:
    mode: sheet            # sheet → read from Google Sheet; file → keywords.txt
    worksheet: Keywords    # tab name
    column: Search Term    # column header to read
  keywords_fallback_file: keywords.txt   # used if the sheet is empty/unreachable
```
**Expect:** `13 keywords from sheet: ['Microbiologist', ...]`.
**Negative test:** set `mode: file` → it loads from `keywords.txt` instead.

---

## Task #1 — Job Validation Service

**Command (quick, 8 rows on the test tab):**
```bash
.venv/bin/python job_validator.py --config config.yaml --worksheet Jobs_Test --limit 8
```
> By default `re_validate: false` skips rows that already have a status. To force a
> real re-check of the 8 rows, flip the YAML key below to `true`, run, then flip back.

**YAML pointers** — `config.yaml`:
```yaml
google_sheets:
  jobs_worksheet: Jobs_Test   # ← which tab to validate
job_validation:
  re_validate: false          # true → re-check every URL; false → skip already-statused rows
http:
  timeout_seconds: 10         # per-request timeout
  max_retries: 3
```
**Expect (console + log):**
```
Validating | worksheet='Jobs_Test' url_col='Job Link' status_col='Job Status' ...
Applying row colors to 8 rows…
Validation done. checked=8 updated=.. | active=.. removed=.. expired=.. unknown=..
```
**Verify in sheet:** `Jobs_Test` → `Job Status` column filled; rows colored
(red=Expired/Removed, yellow=Unknown, white=Active).

**Optional logic-only check (no sheet writes):**
```bash
.venv/bin/python -c "
from job_validator import check_job_url
print('active  ->', check_job_url('https://example.com/'))
print('removed ->', check_job_url('https://github.com/this-does-not-exist-zzz999'))
"
```
**Expect:** `active -> Active`, `removed -> Removed`.

---

## Task #3 — Data Mismatch Flagging

**Command:**
```bash
.venv/bin/python company_validator.py --config config.yaml
```
**YAML pointers** — `config.yaml` (source columns vs enrichment columns):
```yaml
google_sheets:
  company_sheet:
    worksheet: Company
    company_column: Company
    employee_count_column: Avg. Employee-Count   # ← must match the real header
    career_page_column: Career-Page
    linkedin_url_column: Linkedin-Url
  enrichment_output_worksheet: CompaniesTest      # ← automation-written tab being checked
  enrichment_output_columns:
    employee_count: Employee Count
    career_page: Career Page
    linkedin_url: LinkedIn URL
```
**Expect:**
```
Mismatch check | source='Company' vs enrichment='CompaniesTest' | fields=[...]
Compared 241 matched companies | 350 mismatched cells — Employee Count=228  Career Page=121  LinkedIn URL=1
Formatted 350 individual cells
```
**Verify in sheet:** `CompaniesTest` → mismatched cells colored **orange**. Add
`--source <tab> --enrichment <tab>` to compare different tabs.

---

## Tasks #4 & #8 — LinkedIn Pagination / "See More Jobs" Analysis

**Command (single keyword, deep probe):**
```bash
.venv/bin/python pagination_analyzer.py --config config.yaml --keywords "Microbiologist" --max-pages 25
```
**Command (all keywords from the sheet):**
```bash
.venv/bin/python pagination_analyzer.py --config config.yaml
```
**Command (quick: first 3 keywords only):**
```bash
.venv/bin/python pagination_analyzer.py --config config.yaml --limit 3
```
**YAML pointers** — `config.yaml`:
```yaml
pagination_analysis:
  page_size: 10            # LinkedIn returns 10/fetch (must match reality)
  max_probe_pages: 50      # safety cap (50*10 = up to 500 probed/keyword)
  page_delay_seconds: 1.5  # politeness pause between fetches
  keywords_limit: 0        # 0 = all; >0 = first N
  output_csv: logs/pagination_analysis_latest.csv
  write_to_sheet: false    # true → also write report to output_worksheet (a TEST tab)
  output_worksheet: Pagination_Analysis
# related — the scraper fix this analysis drove:
scraping:
  platform_settings:
    linkedin:
      page_size: 10        # ← offset step; 10 avoids skipping jobs
      max_pages: 12
```
**Expect:**
```
[Microbiologist] total=250 jobs across 25 non-empty pages | page1=10 | beyond_page1=240 | hit_cap=yes
PAGINATION ANALYSIS SUMMARY
  Beyond page 1 : 240  (96% of all jobs are behind 'See More Jobs')
Wrote pagination report to 'logs/pagination_analysis_latest.csv'
```
**Verify:** open `logs/pagination_analysis_latest.csv` (one row per keyword). Read-only — it never writes to `Jobs_Test`.

**Prove the scraper fix collects contiguous jobs (no gaps):**
```bash
.venv/bin/python -c "
from config_loader import load_config
from scrapers.linkedin import LinkedInScraper
jobs = LinkedInScraper(load_config('config.yaml')).fetch_jobs('Microbiologist')
links = [j['Job Link'] for j in jobs]
print('collected:', len(jobs), '| unique:', len(set(links)))
"
```
**Expect:** `collected: 120 | unique: 120` (with `max_pages: 12`, `page_size: 10`).

---

## Task #5 — Company vs Institution Classification

**Command (dry run — classify + log, no sheet write):**
```bash
.venv/bin/python company_classifier.py --config config.yaml --dry-run
```
**Command (real run — writes the column + colors rows):**
```bash
.venv/bin/python company_classifier.py --config config.yaml
```
**YAML pointers** — `config.yaml`:
```yaml
classification:
  source_worksheet: CompaniesTest   # ← TEST tab to classify
  name_column: Company
  career_page_column: Career Page   # used for .edu/.gov domain signal
  linkedin_url_column: LinkedIn URL
  industry_column: Industry         # used only if the tab has it (e.g. the Company tab)
  output_column: Organization Type  # auto-created
  write_to_sheet: true              # false (or --dry-run) → no writes
  color_rows: true
```
**Expect:**
```
Classified 619 organizations — Company=500  University=33  Nonprofit / NGO=25  Hospital / Medical=24  Educational Institution=22  Government=9  Research Institute=6
Wrote 619 cells to column 10 (J2:J620)
Applied row colors to 119 rows
```
**Verify in sheet:** `CompaniesTest` → new `Organization Type` column, rows color-coded by category. Report also at `logs/classification_latest.csv` (Company, Organization Type, Reason).

**Spot-check the rules locally:**
```bash
.venv/bin/python -c "
from company_classifier import classify_organization as c
for n in ['Stanford University','Abbott Laboratories','Mayo Clinic','U.S. Department of Energy','Genentech, Inc.']:
    print(c(n)[0], '<-', n)
"
```

---

## Task #7 — Legal & Compliance Review

No runtime command — deliverable is [LEGAL_COMPLIANCE_REVIEW.md](LEGAL_COMPLIANCE_REVIEW.md).
**Verify the codebase claims it makes:**
```bash
git check-ignore secrets/google-service-account.json && echo "secrets git-ignored OK"
grep -rin "robots" --include=*.py . | grep -v __pycache__ || echo "no robots.txt check (gap R2, as documented)"
```

---

## Full pipeline + regression tests

**Run all 4 pipeline steps (enrich → career scrape → keyword scrape → validate):**
```bash
.venv/bin/python automation_pipeline.py --config config.yaml
# or a single step, e.g. validation only:
.venv/bin/python automation_pipeline.py --config config.yaml --only-validation
```
**YAML pointers:** `google_sheets.jobs_worksheet` (target tab), `scraping.platforms`,
`career_pages.*`, `job_validation.re_validate`.

**Unit tests (103 pass) — note the plugin-autoload workaround:**
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_*_unit.py -q -p no:cacheprovider
```
**Expect:** `103 passed`.

---

## After testing

Set logging back to a quieter level for normal runs:
```yaml
logging:
  level: info
```

### Quick reference — command → primary YAML section

| Feature | Command | Primary YAML |
|---------|---------|--------------|
| Keywords from Sheet (#6) | `resolve_keywords` snippet | `scraping.keywords_source` |
| Job Validation (#1) | `job_validator.py` | `google_sheets.jobs_worksheet`, `job_validation` |
| Mismatch Flagging (#3) | `company_validator.py` | `google_sheets.company_sheet`, `enrichment_output_columns` |
| Pagination / See-More (#4, #8) | `pagination_analyzer.py` | `pagination_analysis`, `scraping.platform_settings.linkedin` |
| Classification (#5) | `company_classifier.py` | `classification` |
| Compliance (#7) | doc + verify snippet | — |
| Full pipeline | `automation_pipeline.py` | `scraping`, `career_pages`, `job_validation` |
