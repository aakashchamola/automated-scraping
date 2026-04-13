# Automated Job Scraping Pipeline

A modular, scalable Python pipeline that scrapes job listings from job platforms, deduplicates results across runs, and stores them to a persistent CSV file — designed to grow into a fully automated, agent-driven job hunting system.

---

## Table of Contents

- [Features](#features)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Configuration](#configuration)
- [Keywords](#keywords)
- [Running the Pipeline](#running-the-pipeline)
- [Output](#output)
- [Adding a New Platform](#adding-a-new-platform)
- [Roadmap](#roadmap)

---

## Features

- **Modular architecture** — scraping, storage, and logging are fully separated
- **Dynamic keywords** — read from `keywords.txt`; no code changes needed to update searches
- **Retry mechanism** — automatically retries on HTTP 429, 500, 502, 503, 504 with exponential backoff
- **Granular error handling** — catches timeout, connection errors, and HTTP errors independently
- **Cross-run deduplication** — merges new results with existing history; no duplicate jobs accumulate across runs
- **Timestamped log files** — every run writes to `logs/scrape_YYYYMMDD_HHMMSS.log` alongside console output
- **Platform registry** — add a new scraper in one file with one import; zero changes to pipeline logic
- **Config-driven** — timeouts, retries, output paths, and active platforms all controlled via `config.json`

---

## Project Structure

```
automation/
├── main.py              # Pipeline entry point & platform registry
├── config.json          # All runtime settings
├── keywords.txt         # One search keyword per line
├── logger_setup.py      # Console + rotating file logging setup
├── storage.py           # Load / deduplicate / save CSV logic
├── .gitignore
└── scrapers/
    ├── __init__.py      # BaseScraper abstract base class
    └── indeed.py        # Indeed scraper implementation
```

---

## Setup

### Requirements

- Python 3.9+
- pip packages:

```bash
pip install requests beautifulsoup4 pandas urllib3
```

### Clone

```bash
git clone https://github.com/aakashchamola/automated-scraping.git
cd automated-scraping
```

---

## Configuration

Edit `config.json` to control pipeline behaviour:

```json
{
    "output_file": "jobs.csv",
    "keywords_file": "keywords.txt",
    "request": {
        "timeout": 10,
        "max_retries": 3,
        "retry_delay": 2
    },
    "platforms": ["indeed"]
}
```

| Key | Description |
|---|---|
| `output_file` | CSV file where results are saved and history is maintained |
| `keywords_file` | Path to the keywords file |
| `request.timeout` | Per-request timeout in seconds |
| `request.max_retries` | Number of retry attempts on transient failures |
| `request.retry_delay` | Backoff multiplier in seconds between retries |
| `platforms` | List of active platforms to scrape (must match registry in `main.py`) |

---

## Keywords

Add or remove search terms in `keywords.txt` — one per line:

```
Microbiologist
Molecular Biologist
Research Associate Biology
Clinical Research Associate
Bioinformatics Analyst
```

No code changes needed. The pipeline reads this file fresh on every run.

---

## Running the Pipeline

```bash
# Default — uses config.json
python main.py

# Custom config file
python main.py --config my_config.json
```

### What happens each run

1. Keywords are loaded from `keywords.txt`
2. Each active platform scrapes all keywords
3. New results are merged with existing `jobs.csv` history
4. Duplicates are removed (keyed on `Job Link`)
5. Final deduplicated dataset is saved back to `jobs.csv`
6. A timestamped log is written to `logs/`

---

## Output

`jobs.csv` — cumulative, deduplicated job records:

| Company | Role | Location | Platform | Keyword | Job Link |
|---|---|---|---|---|---|
| Example Labs | Microbiologist | Bangalore | Indeed | Microbiologist | https://in.indeed.com/... |

Logs are written to `logs/scrape_YYYYMMDD_HHMMSS.log` and also printed to console.

---

## Adding a New Platform

**Step 1** — Create `scrapers/<platform>.py`:

```python
from scrapers import BaseScraper

class LinkedInScraper(BaseScraper):
    def fetch_jobs(self, keyword: str) -> list:
        # your scraping logic here
        return [
            {
                "Company": "...",
                "Role": "...",
                "Location": "...",
                "Platform": "LinkedIn",
                "Keyword": keyword,
                "Job Link": "...",
            }
        ]
```

**Step 2** — Register it in `main.py`:

```python
from scrapers.linkedin import LinkedInScraper

SCRAPERS = {
    "indeed": IndeedScraper,
    "linkedin": LinkedInScraper,   # add this
}
```

**Step 3** — Enable it in `config.json`:

```json
"platforms": ["indeed", "linkedin"]
```

That's it. No other changes needed.

---

## Roadmap

- [ ] LinkedIn scraper
- [ ] Naukri.com scraper
- [ ] Company-based job expansion (scrape careers pages directly)
- [ ] Employee scraping for network mapping
- [ ] Filtering logic (salary range, experience level, location)
- [ ] Excel/Google Sheets export
- [ ] Scheduling via cron / Task Scheduler
- [ ] Agent-based automation layer (auto-apply, outreach drafting)
- [ ] Dashboard / notification on new matching jobs
