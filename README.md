# Automated Biotech Job Scraping Pipeline

Collects biotech/life-sciences job postings from multiple job boards and company career pages, deduplicates them, and syncs everything into a shared Google Sheet. A validation service keeps the sheet clean by checking whether each job is still live.

---

## What It Does (Pipeline Overview)

```
automation_pipeline.py
│
├─ Step 1: Company Enrichment       (company_enricher.py)
│    Fills in missing employee counts, career page URLs, and LinkedIn URLs
│    for companies listed in the enrichment output sheet (CompaniesTest).
│
├─ Step 2: Career-Page Scraping     (scrapers/career_page.py)
│    Scrapes job postings directly from each company's own career page.
│    Source: "Career-Page" column in the Company sheet.
│    Filter: only jobs whose title matches a search keyword are kept.
│
├─ Step 3: Keyword Scraping         (main.py)
│    Searches LinkedIn, Indeed, Glassdoor, Lever, Wellfound, YC, SimplyHired,
│    and Internshala for jobs matching each keyword.
│    Keywords come from the "Keywords" Google Sheet tab.
│
└─ Step 4: Job Validation           (job_validator.py)
     Probes every Job Link in the sheet and writes a status:
       Active  – URL is reachable and job is open
       Expired – posting is closed (LinkedIn banner, or other 4xx)
       Removed – URL is gone (404/410)
       Unknown – network error or 5xx; retried next run
     Rows are colored: red = Expired/Removed, yellow = Unknown, white = Active.
```

---

## Google Sheet Structure

Spreadsheet ID: `1SEIHZXpHu6dnjEJ3MHTdljOPhOnLY-2eRzi5vXIYrmA`

| Tab | Type | Purpose |
|-----|------|---------|
| **Jobs** | Production | All scraped job postings |
| **Jobs_Test** | Scratch | Safe testing mirror of Jobs |
| **Company** | Manual | Source of truth for company data (career pages, LinkedIn URLs) |
| **Keywords** | Manual | Search terms for keyword scrapers (`Search Term` column) |
| **CompaniesTest** | Automation output | Enrichment results (employee count, career page, LinkedIn) |
| **People** | Manual | Leads/contacts database — not touched by automation |

> **Rule:** Never point automation at `Jobs` or `Company` until it's tested on `Jobs_Test` / `CompaniesTest` first. Flip `google_sheets.jobs_worksheet` in `config.yaml` to switch.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add your Google service account key
cp your-key.json secrets/google-service-account.json

# 3. Edit config.yaml — at minimum set:
#      google_sheets.enabled: true
#      google_sheets.jobs_worksheet: Jobs_Test   ← keep this for testing

# 4. Run the full pipeline
python automation_pipeline.py

# — or run individual steps —
python main.py                                # keyword scraping only
python job_validator.py                       # validation only
python company_enricher.py                    # company enrichment only
```

---

## Configuration (`config.yaml`)

Config is in **YAML** format so you can add `#` comments. All sections are documented inside the file itself.

### Key sections at a glance

| Section | What it controls |
|---------|-----------------|
| `logging.level` | Log verbosity: `debug` / `info` / `warning` / `error` |
| `http.*` | Timeout, retries, delay between requests |
| `scraping.*` | Output CSV, keywords source, active platforms, per-platform settings |
| `career_pages.*` | Which sheet tab and columns hold career page URLs |
| `job_validation.*` | Whether to re-check already-validated rows (`re_validate`) |
| `google_sheets.*` | Credentials, spreadsheet ID, which tabs to read/write |

### Switching from testing to production

In `config.yaml`, change:
```yaml
google_sheets:
  jobs_worksheet: Jobs_Test   # ← testing
  # jobs_worksheet: Jobs      # ← production (uncomment when ready)
```

### Adding a new search keyword

Add a row to the **Keywords** tab in Google Sheets (`Search Term` column). The pipeline reads it automatically on the next run.

### Adding a new job-board platform

1. Create `scrapers/<platform>.py` implementing `BaseScraper.fetch_jobs(keyword)`
2. Import it in `main.py` and add it to `SCRAPERS`
3. Add the platform name to `scraping.platforms` in `config.yaml`

---

## File Structure

```
automated-scraping/
│
├── config.yaml                  ← main config (YAML with comments)
├── config_loader.py             ← shared YAML/JSON loader used by all entry points
│
├── automation_pipeline.py       ← master runner: orchestrates all 4 steps
├── main.py                      ← keyword scraping pipeline entry point
├── job_validator.py             ← job URL validation entry point
├── company_enricher.py          ← company data enrichment entry point
│
├── google_sheets_store.py       ← Google Sheets read/write/format layer
├── storage.py                   ← local CSV deduplication and save/load
├── logger_setup.py              ← timestamped log file per run (logs/<name>_<ts>.log)
│
├── scrapers/
│   ├── linkedin.py              ← LinkedIn keyword scraper
│   ├── indeed.py                ← Indeed scraper
│   ├── glassdoor.py             ← Glassdoor scraper
│   ├── lever.py                 ← Lever ATS scraper
│   ├── wellfound.py             ← Wellfound / AngelList scraper
│   ├── ycombinator.py           ← Y Combinator jobs scraper
│   ├── simplyhired.py           ← SimplyHired scraper
│   ├── internshala.py           ← Internshala scraper
│   ├── career_page.py           ← Career-page scraper (ATS detection + HTML fallback)
│   └── http_utils.py            ← Shared session builder with retry logic
│
├── enricher/
│   ├── employee.py              ← Employee count scraping (LinkedIn, with UA rotation)
│   ├── career.py                ← Career page discovery (LinkedIn slug → DDG fallback)
│   ├── search.py                ← Multi-engine search (DDG → Bing → Mojeek)
│   ├── linkedin.py              ← LinkedIn profile fetching
│   ├── normalizers.py           ← URL normalization helpers
│   ├── source_sheet.py          ← Reads company data from the Company sheet
│   ├── sheets.py                ← Low-level Sheets API helpers for enricher
│   └── config.py                ← Config key extraction with safe defaults
│
├── tests/
│   ├── test_job_validator_unit.py
│   ├── test_career_page_unit.py
│   ├── test_sheets_align_unit.py
│   ├── test_normalizers_unit.py
│   ├── test_config_unit.py
│   ├── test_source_sheet_unit.py
│   └── smoke_test.py            ← Live single-keyword run across all platforms
│
├── logs/                        ← Timestamped log files per run
├── secrets/                     ← Google service account JSON (git-ignored)
├── keywords.txt                 ← Fallback keywords when sheet is unavailable
└── requirements.txt
```

---

## Logs

Every entry point writes its own named, timestamped log:

| Entry point | Log prefix |
|------------|------------|
| `automation_pipeline.py` | `logs/pipeline_<ts>.log` |
| `main.py` | `logs/scrape_<ts>.log` |
| `job_validator.py` | `logs/validate_<ts>.log` |
| `company_enricher.py` | `logs/enrich_<ts>.log` |

---

## Job Validation Command

```bash
# Validate all rows (re-check every URL)
python job_validator.py --config config.yaml --worksheet Jobs_Test

# Re-color rows using existing statuses (no HTTP checks)
# Set re_validate: false in config.yaml, then:
python job_validator.py --config config.yaml --worksheet Jobs_Test

# Quick test on first N rows
python job_validator.py --config config.yaml --worksheet Jobs_Test --limit 10
```

Row colors after validation:
- ⬜ **White** — Active
- 🔴 **Red** — Expired or Removed
- 🟡 **Yellow** — Unknown (network error; retry on next run)
