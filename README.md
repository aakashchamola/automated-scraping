# Automated Job Scraping Pipeline — Technical Reference

A modular, config-driven Python pipeline that scrapes job listings from multiple platforms, deduplicates results, writes to CSV, and optionally syncs live to Google Sheets.

> For step-by-step setup instructions after cloning, see [SETUP.md](SETUP.md).

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

Sets up two handlers:

| Handler | Output | Level |
|---|---|---|
| `StreamHandler` | stdout | INFO |
| `FileHandler` | `logs/run_YYYYMMDD_HHMMSS.log` | DEBUG |

`logs/` directory is created automatically on first run.

---

### `config.json` — Runtime Configuration

Full schema with all supported keys:

```json
{
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
    "worksheet": "Jobs"
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
| `google_sheets.worksheet` | Tab name inside the spreadsheet |

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
# Standard run
python main.py

# Custom config
python main.py --config config.json

# Quick platform health check (one keyword, all platforms)
python smoke_test.py
```

---

## Roadmap

- [x] Phase 1: base pipeline (Indeed + CSV + dedupe + logs)
- [x] Configurable country/location
- [x] Multi-platform scraping layer (8 platforms)
- [x] Google Sheets live sync (dedupe-safe append)
- [ ] Scheduled runs (cron / GitHub Actions)
- [ ] Relevance filtering / scoring
- [ ] Enrichment layer (company info, salary estimates)
- [ ] Orchestrator layer coordinating scrape → enrich → action
