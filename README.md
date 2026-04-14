# Automated Job Scraping Pipeline

This project collects job listings in a clean, repeatable way.

Right now it is built for **Phase 1**:
1. Scrape jobs from **Indeed**
2. Use a keyword list from file
3. Save results to CSV
4. Keep data clean by removing duplicates

The long-term goal is to grow this into an **orchestrated automation pipeline**.

---

## Current Progress (April 2026)

What is done so far:
1. Project refactored into clean modules (scraper, storage, logging, config)
2. Indeed scraper is working with retry and error handling
3. Country and location are configurable (currently set to US)
4. Keywords are fully configurable from `keywords.txt`
5. Output is generated in `jobs.csv` with fixed columns
6. Deduplication is enabled so repeated runs do not keep adding the same jobs
7. Run logs are saved to `logs/`

Current output columns:
1. Company
2. Role
3. Location
4. Platform
5. Job Link
6. Keyword

---

## What We Are Building Next

Near-term direction:
1. Add more platforms (LinkedIn, Glassdoor, etc.)
2. Improve filtering and relevance
3. Add scheduled runs (daily/weekly)
4. Add orchestrator layer to manage multi-step automation

Final direction:
1. Scrape -> clean -> enrich -> orchestrate actions from one pipeline

---

## Project Structure

```
automated-scraping/
├── main.py
├── config.json
├── keywords.txt
├── logger_setup.py
├── storage.py
├── requirements.txt
├── jobs.csv                  # created after running
├── logs/                     # created after running
└── scrapers/
    ├── __init__.py
    └── indeed.py
```

---

## Quick Setup

Run these commands from the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

If you are on macOS with Homebrew Python, always use this `.venv` flow.

---

## Basic Configuration

Edit `config.json`:

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
  "platforms": ["indeed"],
  "platform_settings": {
    "indeed": {
      "country": "us",
      "location": "United States",
      "max_pages": 1
    }
  }
}
```

What to edit most often:
1. `keywords.txt` for search terms
2. `platform_settings.indeed.country` for market (`us`, `in`, `uk`, `ca`, `au`)
3. `platform_settings.indeed.location` for region/city/state
4. `output_file` if you want a different CSV name

---

## How to Run (Day-to-Day)

```bash
source .venv/bin/activate
python main.py
```

After run:
1. Check `jobs.csv` for job data
2. Check `logs/` for run logs

---

## Notes for Refactor + Orchestrator Goal

This codebase is intentionally kept simple in Phase 1.

The current refactor already supports easy growth:
1. New scrapers can be added under `scrapers/`
2. Main pipeline logic remains mostly unchanged
3. Storage stays consistent as platforms expand

When we add the orchestrator, it will sit on top of this pipeline and coordinate steps like:
1. Run platform scrapers
2. Merge and dedupe output
3. Trigger enrichment/filtering
4. Trigger follow-up actions

---

## Roadmap

- [x] Phase 1 base pipeline (Indeed + CSV + dedupe + logs)
- [x] Configurable country/location for Indeed
- [ ] Add 1 more platform
- [ ] Introduce scheduling
- [ ] Add relevance filtering
- [ ] Integrate orchestrator layer
