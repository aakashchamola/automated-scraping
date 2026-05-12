# Automated Job Scraping Pipeline — Technical Reference

A modular, config-driven Python pipeline that scrapes job listings from multiple platforms, deduplicates results, writes to CSV, and optionally syncs live to Google Sheets. A second standalone automation enriches company-level data (employee count, career page, LinkedIn URL) into a dedicated Google Sheet tab.

> For step-by-step setup instructions after cloning, see [SETUP.md](SETUP.md).

---

## Project Structure

```
automated-scraping/
├── main.py                   # Job scraping pipeline entry point
├── company_enricher.py       # Standalone company enrichment automation
├── cleanup_validation.py      # Safe cleanup of stale target company rows
├── google_sheets_store.py    # Google Sheets sink/source for jobs pipeline
├── storage.py                # Schema normalisation, dedup, CSV persistence
├── logger_setup.py           # Shared logging setup (file + stdout)
├── config.json               # Runtime configuration for all scripts
├── ENRICHER_CONFIG_GUIDE.md  # Detailed enrichment behavior/config guide
├── keywords.txt              # One keyword per line for job searches
├── requirements.txt          # Python dependencies
│
├── enricher/                 # Modular helpers used by company_enricher.py
│   ├── config.py
│   ├── normalizers.py
│   ├── sheets.py
│   ├── source_sheet.py
│   ├── linkedin.py
│   ├── employee.py
│   └── career.py
│
├── scrapers/                 # Platform-specific job scrapers
│   ├── __init__.py           # BaseScraper abstract class
│   ├── indeed.py
│   ├── linkedin.py
│   └── ...                   # glassdoor, lever, wellfound, etc.
│
├── secrets/                  # Credentials (gitignored)
│   └── google-service-account.json
│
├── csv/                      # CSV data output directory
│   └── jobs.csv
│
├── logs/                     # All log files (gitignored)
│   └── scrape_YYYYMMDD_HHMMSS.log
│
└── tests/                    # Ad-hoc test and debug scripts
```

---

## Architecture Overview

```
config.json + keywords.txt
        │
        ▼
      main.py  ──── loads config, validates, reads keywords
        │
        ├── for each platform:
        │       ScraperClass(config).fetch_jobs(keyword)
        │               └── returns list of job dicts
        │
        ├── storage.py
        │       ├── prepare_df()      — normalise columns + types
        │       ├── deduplicate()     — merge new + existing, remove dupes
        │       ├── get_new_rows()    — delta: rows not in existing
        │       └── save()            — write final CSV
        │
        └── google_sheets_store.py   (optional)
                ├── load_existing()  — read current sheet rows
                └── append_rows()   — append only new rows
```

Data flows in one direction: scrape → normalise → deduplicate → persist.

---

## Module Breakdown

### `main.py` — Orchestrator

The entry point. Responsibilities:

- Parse CLI args (`--config`)
- Load and validate `config.json`
- Load keywords from `keywords.txt`
- Iterate platforms × keywords, calling each scraper
- Merge all collected jobs into one DataFrame
- Read existing CSV and (optionally) Google Sheet as prior state
- Deduplicate combined results and write back to CSV
- Append only net-new rows to Google Sheet

**Platform registry** (`SCRAPERS` dict): maps platform name strings from config to scraper classes. Adding a new platform only requires adding one entry here.

```python
SCRAPERS = {
    "indeed":      IndeedScraper,
    "linkedin":    LinkedInScraper,
    "glassdoor":   GlassdoorScraper,
    "jobs.lever":  LeverScraper,
    "internshala": InternshalaScraper,
    "wellfound":   WellfoundScraper,
    "ycombinator": YCombinatorScraper,
    "simplyhired": SimplyHiredScraper,
}
```

**Error isolation**: scraper init and per-keyword fetch are each wrapped in try/except so one failing platform or keyword does not abort the whole run.

---

### `company_enricher.py` — Company Enrichment Automation

A fully standalone script (independent of `main.py`) that reads the **Companies** tab of your Google Sheet and fills in company-level data.

**Sheet columns populated:**

| Column | Header | Content |
|--------|--------|---------|
| A | Company | Company name (set by sync from Jobs tab) |
| B | Employee-Count | Headcount scraped from LinkedIn |
| C | Career-Page | Company careers URL |
| D | LinkedIn-URL | Discovered LinkedIn company page URL |

**Processing rules:**
- Row 1 is always the header row; data starts at row 2.
- Enrichment behavior is controlled by three independent config features:
  - `google_sheets.validation.enabled`
  - `google_sheets.validation.retry_na_fields`
  - `google_sheets.source_sheet.enabled` (+ source fallback toggles)
- Depending on config, rows may be fully re-validated, NA fields may be retried, or only empty fields may be processed.
   - Source-sheet LinkedIn fallback can also read `Job Link` when `google_sheets.source_sheet.job_link_header` is set.
- When a value cannot be found, `NA` is written to that cell and the cell background is colored **red** — so you can see at a glance what needs manual attention.
- Idempotent: safe to run multiple times; already-filled cells are never overwritten.

**Tasks performed on each run:**
1. **Task 3 — Sync:** Pulls unique company names from the Jobs tab into the Companies tab (no duplicates added).
2. **Task 1 — Employee count:** Probes the LinkedIn company page and extracts headcount from the page's structured data.
3. **Task 2 — Career page:** Extracts the company website from LinkedIn structured data, then probes common career paths (`/careers`, `careers.{domain}`, etc.).

**LinkedIn URL discovery:** Generates likely URL slugs from the company name and probes `linkedin.com/company/{slug}/` directly. No third-party search engine required.

**Resilience:** All Google Sheets write operations (`update`, `format`) are wrapped with automatic retry — up to 4 attempts with exponential backoff (8 s, 16 s, 32 s, 64 s) on transient network errors (`ConnectionResetError`, timeouts) and API rate-limit responses (HTTP 429/5xx). The script will not crash on a single dropped connection.

For full enrichment behavior and profile examples, see [ENRICHER_CONFIG_GUIDE.md](ENRICHER_CONFIG_GUIDE.md).

**Usage:**
```bash
python company_enricher.py
python company_enricher.py --config config.json --companies-sheet "Companies"

# Run in background and log output
nohup python company_enricher.py > logs/enrichment_run.log 2>&1 &
```

---

### `enricher/` — Modular Enrichment Package

`company_enricher.py` orchestrates flow and delegates logic to focused modules:

- `enricher/config.py`: parses validation/source/enrichment controls and column/header config.
- `enricher/normalizers.py`: normalizes/validates LinkedIn URLs, career URLs, and employee count formats.
- `enricher/sheets.py`: sheet header/index helpers and column hyperlink reads.
- `enricher/source_sheet.py`: source worksheet readers for company/linkedin/career/employee mappings.
- `enricher/linkedin.py`: LinkedIn slug discovery and profile status checks.
- `enricher/employee.py`: employee count scraping.
- `enricher/career.py`: website extraction (including LinkedIn `/about/`) and career-page probing.

---

### `scrapers/__init__.py` — `BaseScraper` (Abstract Base)

All scrapers inherit from `BaseScraper`:

```python
class BaseScraper(ABC):
    def __init__(self, config: dict) -> None: ...

    @abstractmethod
    def fetch_jobs(self, keyword: str) -> list[dict]: ...
```

Every `fetch_jobs` implementation must return a list of dicts with at minimum these keys:

| Key | Description |
|---|---|
| `Company` | Employer name |
| `Role` | Job title |
| `Location` | Location string |
| `Platform` | Platform name (e.g. `"indeed"`) |
| `Job Link` | Direct URL to posting |
| `Keyword` | The keyword used to find this job |

---

### `scrapers/` — Platform Scrapers

| File | Class | Method |
|---|---|---|
| `indeed.py` | `IndeedScraper` | HTML scraping, CSS selectors, configurable country/domain |
| `linkedin.py` | `LinkedInScraper` | HTML scraping, public job search endpoint |
| `glassdoor.py` | `GlassdoorScraper` | HTML scraping; may be blocked by anti-bot |
| `lever.py` | `LeverScraper` | JSON API (`/api/v0/postings/<slug>`); needs `sites` list in config |
| `internshala.py` | `InternshalaScraper` | HTML scraping |
| `wellfound.py` | `WellfoundScraper` | HTML scraping; may be blocked by anti-bot |
| `ycombinator.py` | `YCombinatorScraper` | HTML scraping from WorkAtAStartup; best-effort |
| `simplyhired.py` | `SimplyHiredScraper` | HTML scraping |

**Reliability notes:**
- Consistently returning data in tests: LinkedIn, Indeed, Internshala, SimplyHired
- Fragile due to anti-bot/Cloudflare: Glassdoor, Wellfound, Y Combinator
- Lever requires `platform_settings.lever.sites` to be populated with company slugs (e.g. `["stripe", "notion"]`)

**HTTP layer (`scrapers/http_utils.py`):** shared session builder with `urllib3.Retry` on status codes `429, 500, 502, 503, 504`. All scrapers that need retry logic use this.

---

### `storage.py` — Normalisation, Deduplication, Persistence

**Schema:** every DataFrame is normalised to exactly these columns before any operation:

```
Company | Role | Location | Platform | Job Link | Keyword
```

**`prepare_df(df)`**
- Adds any missing columns as empty strings
- Strips whitespace, fills NaN with `""`
- Returns DataFrame with fixed column order

**`deduplicate(new_df, existing_df)`**
- Merges new + existing into one combined DataFrame
- Primary dedup key: `(Platform, Job Link)` — used when `Job Link` is non-empty
- Fallback dedup key: `(Platform, Company, Role)` — used when `Job Link` is empty
- `keep="last"` so fresh scrape data takes precedence over stale data

**`get_new_rows(new_df, existing_df)`**
- Returns only rows from `new_df` that do not appear in `existing_df`
- Used to compute the delta for Google Sheets append (avoids re-writing existing rows)

**`save(df, filepath)`**
- Writes the final deduplicated DataFrame to CSV

---

### `google_sheets_store.py` — Google Sheets Sink/Source

Activated only when `config.google_sheets.enabled = true`.

**`load_existing()`** — reads all rows currently in the worksheet and returns them as a DataFrame. Used as prior state before deduplication.

**`append_rows(df)`** — appends only the provided rows to the sheet. Never rewrites the whole sheet.

**`_get_worksheet()`** — lazy initialiser. Authenticates via service account JSON, opens spreadsheet by ID, resolves or creates the named worksheet.

**`_ensure_header(worksheet)`** — writes the column header row on first use if the sheet is empty.

**Authentication flow:**
1. Load `credentials_file` (service account JSON)
2. Build `google.oauth2.service_account.Credentials` with Sheets + Drive scopes
3. Authorize `gspread` client
4. Open spreadsheet by `spreadsheet_id`
5. Resolve worksheet by name; create it if it doesn't exist

**Error surface:**
- `RuntimeError` with an actionable message if credentials are missing, spreadsheet ID is missing, or a 403 is returned (sheet not shared with service account)

---

### `logger_setup.py` — Logging

Centralized logger service used by both `main.py` and `company_enricher.py`.

Sets up two handlers:

| Handler | Output | Level |
|---|---|---|
| `StreamHandler` | stdout | from config |
| `FileHandler` | `logs/scrape_YYYYMMDD_HHMMSS.log` | from config |

`logs/` directory is created automatically on first run.

Log level is controlled from `config.json`:

```json
"logging": {
  "level": "info"
}
```

Supported values are `info` and `debug` (case-insensitive).

- `info`: keeps current high-level operational logs.
- `debug`: adds full value-level tracing for source and target sheet checks/replacements.

---

### `config.json` — Runtime Configuration

Full schema with all supported keys:

```json
{
  "logging": {
    "level": "info"
  },
  "output_file": "jobs.csv",
  "keywords_file": "keywords.txt",

  "request": {
    "timeout": 10,
    "max_retries": 3,
    "retry_delay": 1,
    "delay_between_requests": 0
  },

  "platforms": [
    "linkedin", "indeed", "glassdoor", "jobs.lever",
    "internshala", "wellfound", "ycombinator", "simplyhired"
  ],

  "platform_settings": {
    "indeed": {
      "country": "us",
      "location": "United States",
      "max_pages": 1
    },
    "linkedin": { "location": "United States", "max_pages": 1 },
    "glassdoor": { "location": "United States", "max_pages": 1, "loc_id": "" },
    "lever": { "location": "United States", "sites": [] },
    "internshala": { "location": "United States", "max_pages": 1 },
    "wellfound": { "location": "United States", "max_pages": 1 },
    "ycombinator": { "location": "United States", "max_pages": 1 },
    "simplyhired": { "location": "United States", "max_pages": 1 }
  },

  "google_sheets": {
    "enabled": false,
    "credentials_file": "secrets/google-service-account.json",
    "spreadsheet_id": "",
    "worksheet": "Jobs",
    "companies_worksheet": "Companies",
    "source_sheet": {
      "enabled": true,
      "worksheet": "Company",
      "company_header": "Company",
      "employee_count_header": "Employee-Count",
      "career_page_header": "Career-Page",
      "linkedin_url_header": "Linkedin-Url",
      "job_link_header": "Job Link",
      "use_for_company_sync": true,
      "use_for_linkedin_fallback": true,
      "use_for_career_fallback": true
    },
    "validation": {
      "enabled": false,
      "retry_na_fields": false
    },
    "enrichment_controls": {
      "retry_invalid_career_values": ["://", ""]
    },
    "companies_columns": {
      "company": "Company",
      "employee_count": "Employee Count",
      "career_page": "Career Page",
      "linkedin_url": "LinkedIn URL"
    }
  }
}
```

| Key | Purpose |
|---|---|
| `output_file` | Path to CSV output |
| `keywords_file` | Path to keyword list (one keyword per line) |
| `request.timeout` | Per-request timeout in seconds |
| `request.max_retries` | Retry attempts on transient errors |
| `request.retry_delay` | Backoff factor (seconds) between retries |
| `request.delay_between_requests` | Sleep between successive requests (rate limit safety) |
| `platforms` | Ordered list of platforms to run |
| `platform_settings.<name>.max_pages` | How many result pages to fetch per keyword |
| `platform_settings.indeed.country` | Two-letter country code (`us`, `in`, `uk`, `ca`, `au`) |
| `platform_settings.lever.sites` | List of company slugs for Lever API (e.g. `["stripe"]`) |
| `google_sheets.enabled` | Toggle Sheets integration |
| `google_sheets.credentials_file` | Path to service account JSON |
| `google_sheets.spreadsheet_id` | ID from the Google Sheet URL |
| `google_sheets.worksheet` | Jobs tab name inside the spreadsheet |
| `google_sheets.companies_worksheet` | Companies tab name for the enrichment automation |
| `google_sheets.source_sheet.*` | Source worksheet controls + header mappings for sync/fallback |
| `google_sheets.source_sheet.job_link_header` | Optional Jobs-tab column used to extract LinkedIn company URLs |
| `google_sheets.validation.*` | Validation master switch + NA retry behavior |
| `google_sheets.enrichment_controls.retry_invalid_career_values` | Values treated as invalid and eligible for retry |
| `google_sheets.companies_columns.*` | Destination Companies worksheet header names |

---

## Adding a New Scraper

1. Create `scrapers/<platform>.py`
2. Subclass `BaseScraper` and implement `fetch_jobs(keyword) -> list[dict]`
3. Each returned dict must include: `Company`, `Role`, `Location`, `Platform`, `Job Link`, `Keyword`
4. Register in `main.py` SCRAPERS dict: `"platform_name": MyScraperClass`
5. Add `"platform_name"` to `platforms` in `config.json`

---

## Data Flow Diagram

```
keywords.txt    config.json
     │               │
     └───────┬────────┘
             ▼
          main.py
             │
     ┌───────┴────────────────────────┐
     │  for platform in platforms:    │
     │    for keyword in keywords:    │
     │      scraper.fetch_jobs(kw)    │
     └───────────────────────────────┘
             │
             ▼ list of job dicts
          pd.DataFrame (new_df)
             │
     ┌───────┴──────────────────────────────┐
     │  storage.load_existing(csv)          │
     │  google_sheets_store.load_existing() │
     │  → existing_combined_df             │
     └──────────────────────────────────────┘
             │
             ▼
     storage.deduplicate(new_df, existing_combined_df)
             │
             ├── storage.save()            → jobs.csv
             │
             └── google_sheets_store.append_rows(delta)  → Google Sheet
```

---

## Running

```bash
# Job scraping pipeline
python main.py
python main.py --config config.json

# Company enrichment (separate automation)
python company_enricher.py
python company_enricher.py --companies-sheet CompaniesTest

# Cleanup target rows that are not in Jobs/Company reference sheets
python cleanup_validation.py
python cleanup_validation.py --config cleanup_validation_config.json

# Run enrichment in background with log
nohup python company_enricher.py > logs/enrichment_run.log 2>&1 &
nohup python cleanup_validation.py > logs/cleanup_validation_run.log 2>&1 &

`cleanup_validation.py` behavior summary:
- Reads unique company names from `Jobs` and `Company` tabs.
- Builds union of those names as the allowed set.
- Appends missing allowed company names into the target sheet when `cleanup_validation.behavior.sync_missing_to_target=true`.
- In `CompaniesTest`, clears rows whose company is not in that allowed set.
- Resets cleared row background to white.
- Never writes to `Jobs` or `Company` tabs.
- Uses timestamped backup/report CSV files under `logs/` by default.
- Default cleanup config uses `dry_run=true`; set it to `false` only after previewing the output.
# Quick platform health check (one keyword, all platforms)
python tests/smoke_test.py
```

---

## Roadmap

- [x] Phase 1: base pipeline (Indeed + CSV + dedupe + logs)
- [x] Configurable country/location
- [x] Multi-platform scraping layer (8 platforms)
- [x] Google Sheets live sync (dedupe-safe append)
- [x] Company enrichment automation (employee count, career page, LinkedIn URL)
- [x] NA sentinel + red cell highlighting for unresolvable companies
- [ ] Scheduled runs (cron / GitHub Actions)
- [ ] Relevance filtering / scoring
- [ ] Enrichment layer (company info, salary estimates)
- [ ] Orchestrator layer coordinating scrape → enrich → action
