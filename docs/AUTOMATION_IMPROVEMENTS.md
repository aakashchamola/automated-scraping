# Automation Improvements

This document covers three new automation features:
1. **Job Validation Service** - Keep jobs up-to-date
2. **Career Page Scraper** - Scrape from company career pages
3. **Master Automation Pipeline** - Run everything in sequence

---

## 1. Job Validation Service (`job_validator.py`)

### Purpose
Periodically check if job postings are still active. Marks jobs as:
- **Active** - URL is accessible (HTTP 200)
- **Expired** - URL redirects or is inaccessible
- **Removed** - Job link returns 404
- **Unknown** - Server error or network issue

### Setup

#### A. Add Status Column to Google Sheets
1. Open your "Jobs" worksheet
2. Add a new column: `Job Status`
3. Save the sheet

#### B. Run Validator
```bash
python job_validator.py --config config.json
```

Optional flags:
```bash
python job_validator.py \
  --config config.json \
  --worksheet "Jobs" \
  --url-column "Job Link" \
  --status-column "Job Status" \
  --max-age-days 90
```

### How It Works
1. Reads all rows from the Jobs sheet
2. For each job URL, checks if the link is still live
3. Updates the "Job Status" column with the result
4. Logs validation results to logs/

### Scheduling (Google Cloud Scheduler / Cron)
```bash
# Run validation daily at 2 AM
0 2 * * * cd /path/to/automated-scraping && python job_validator.py --config config.json
```

### Output Example
```
INFO job_validator: Row 2: Updated status to 'Active'
INFO job_validator: Row 5: Updated status to 'Removed' (404 returned)
INFO job_validator: Row 8: Updated status to 'Expired' (auth required)
INFO job_validator: Validation complete. Checked 156 jobs, updated 42 statuses.
```

---

## 2. Career Page Scraper (`scrapers/career_page.py`)

### Purpose
Scrape job postings directly from company career pages listed in your Companies database.

**Why use this?**
- Companies often post jobs on their careers page before job boards
- Direct scraping is more reliable than keywords
- Pulls from your enriched company data (career page URLs)

### How It Works

1. **Reads company data** from the Companies sheet
   - Extracts company names and career page URLs
   - Uses data enriched by `company_enricher.py`

2. **Scrapes career pages** for job links
   - Uses CSS selectors (modern ATS platforms)
   - Falls back to regex patterns
   - Extracts 20 most recent jobs per company

3. **Returns standardized job objects**
   ```python
   {
     "title": "Job at Stripe",
     "company": "Stripe",
     "job_link": "https://stripe.com/careers/jobs/...",
     "platform": "career_page",
     "location": ""
   }
   ```

4. **Appends to Jobs sheet**

### Supported Career Page Platforms
- **Custom careers pages** (any HTML-based career page)
- **Greenhouse.io** (common ATS)
- **Lever.co** (common ATS)
- **Ashby** (modern ATS)
- Any other ATS that exposes job links in HTML

### Integration with Automation Pipeline
The career page scraper is built into the master `automation_pipeline.py`:

```bash
python automation_pipeline.py --config config.json
```

Or run standalone (not recommended):
```python
from scrapers.career_page import scrape_companies
from google_sheets_store import GoogleSheetsStore

# Load companies
store = GoogleSheetsStore(config["google_sheets"])
companies = store.load_all_rows("Companies")

# Scrape
jobs = scrape_companies(companies, company_col="Company", career_col="Career Page")
print(f"Found {len(jobs)} jobs")
```

### Example Output
```
INFO career_page: Scraped 12 jobs from Stripe career page: https://stripe.com/careers
INFO career_page: Scraped 5 jobs from Google career page: https://careers.google.com
INFO career_page: Scraped 8 jobs from Microsoft career page: https://careers.microsoft.com
Total: 25 jobs from 3 companies
```

---

## 3. Master Automation Pipeline (`automation_pipeline.py`)

### Purpose
Orchestrates all automation tasks in a logical sequence:

```
Company Enrichment
    ↓
Career Page Scraping (uses enriched data)
    ↓
Keyword-Based Scraping (LinkedIn, Indeed, etc.)
    ↓
Job Validation (mark active/expired)
    ↓
Save to CSV + Google Sheets
```

### Usage

#### Full Pipeline (All Steps)
```bash
python automation_pipeline.py --config config.json
```

#### Skip Steps as Needed
```bash
# Skip enrichment (companies already populated)
python automation_pipeline.py --config config.json --skip-enrichment

# Skip career scraping (only do keyword platforms)
python automation_pipeline.py --config config.json --skip-career-scraping

# Skip keyword scraping (only do career pages)
python automation_pipeline.py --config config.json --skip-keyword-scraping

# Skip validation (faster if you just added jobs)
python automation_pipeline.py --config config.json --skip-validation

# Combine multiple
python automation_pipeline.py --config config.json --skip-enrichment --skip-validation
```

### Step-by-Step Execution

**Step 1: Company Enrichment** (5-10 minutes)
- Calls `company_enricher.py`
- Fills missing employee counts, career pages, LinkedIn URLs
- Result: Your Companies sheet is up-to-date

**Step 2: Career Page Scraping** (2-5 minutes)
- For each company with a career page URL
- Extracts job links from the career page
- Appends new jobs to the Jobs sheet

**Step 3: Keyword-Based Scraping** (10-20 minutes)
- Runs your standard keyword scraping across platforms
- LinkedIn, Indeed, Glassdoor, etc.
- Deduplicates against existing jobs

**Step 4: Job Validation** (5-15 minutes)
- Checks if all job URLs are still active
- Updates Job Status column
- Marks expired/removed jobs

### Scheduling (Google Cloud Scheduler / Cron)

```bash
# Run full pipeline daily at 1 AM
0 1 * * * cd /path/to/automated-scraping && python automation_pipeline.py --config config.json

# Run validation-only daily at 10 PM (lighter load)
0 22 * * * cd /path/to/automated-scraping && python automation_pipeline.py --config config.json --skip-enrichment --skip-career-scraping --skip-keyword-scraping
```

### Log Output Example
```
================================================================================
AUTOMATION PIPELINE START
================================================================================
================================================================================
STEP 1: Company Enrichment
================================================================================
INFO company_enricher: Starting enrichment of 50 companies...
INFO company_enricher: Row 5: Enriched 'Google' with 150,000+ employees
✓ Company enrichment complete

================================================================================
STEP 2: Career Page Scraping
================================================================================
INFO career_page: Scraped 12 jobs from Stripe
INFO career_page: Scraped 8 jobs from Microsoft
INFO career_page: Scraped 15 jobs from Amazon
✓ Found 35 jobs from career pages

================================================================================
STEP 3: Keyword-Based Job Scraping
================================================================================
INFO main: LinkedIn | keyword='internship' | collected=23
INFO main: Indeed | keyword='internship' | collected=34
✓ Keyword scraping complete

================================================================================
STEP 4: Job Validation
================================================================================
INFO job_validator: Validated 200 jobs...
INFO job_validator: Row 45: Updated status to 'Removed'
INFO job_validator: Validation complete. Checked 230 jobs, updated 12 statuses.
✓ Job validation complete

================================================================================
✓ AUTOMATION PIPELINE COMPLETE
================================================================================
```

### Config Updates

Your config.json already has everything needed! But you can add:

```json
{
  "automation": {
    "enable_enrichment": true,
    "enable_career_scraping": true,
    "enable_keyword_scraping": true,
    "enable_validation": true,
    "validation_max_age_days": 90,
    "career_page_timeout_sec": 15,
    "career_page_max_jobs_per_company": 20
  }
}
```

---

## Integration Example

### End-to-End Workflow

**Day 1: Initial Setup**
```bash
# Build your company database (manually or via another data source)
# In Google Sheets, populate Companies sheet with company names only

# Run enrichment to fill in career pages, employee counts, etc.
python automation_pipeline.py --config config.json --skip-keyword-scraping --skip-validation

# Check Google Sheets Companies sheet — should now have enriched data
```

**Day 2+: Daily Automation**
```bash
# Run full pipeline daily
0 1 * * * python automation_pipeline.py --config config.json
```

**Weekly: Validation-Only Run**
```bash
# Check job validity without re-scraping (faster)
0 22 * * 0 python automation_pipeline.py --config config.json --skip-enrichment --skip-career-scraping --skip-keyword-scraping
```

---

## Troubleshooting

### Q: Career page scraper finds no jobs
**A:** Check the career page URL in your Companies sheet. Verify:
- URL is publicly accessible
- The URL actually hosts job listings
- Check logs for specific errors: `logs/scrape_TIMESTAMP.log`

### Q: Validation marks all jobs as "Unknown"
**A:** Likely network or Google Sheets rate limiting. Check:
- Network connectivity
- Google Sheets API quota
- Logs for rate limit errors
- Increase `--max-age-days` if too many old jobs

### Q: Career page scraper is slow
**A:** It intentionally sleeps 2 seconds between companies (polite scraping).
- Adjust `time.sleep(2)` in `scrapers/career_page.py` if needed
- Reduce `max_jobs_per_company` in career page scraper

### Q: Can I run steps in parallel?
**A:** Not recommended. Run in sequence to avoid sheet conflicts.
- Enrichment must run before career scraping (enriched data needed)
- Scraping must complete before validation

---

## Next Steps for Your Boss

1. **Setup**: Add "Job Status" column to Jobs sheet
2. **Run**: Execute `python automation_pipeline.py --config config.json`
3. **Monitor**: Check logs in `logs/` folder
4. **Schedule**: Set up cron job for daily automation (optional but recommended)
5. **Review**: Check Google Sheets for enriched data and fresh jobs weekly

---

## Files Modified/Added

```
NEW:
  - job_validator.py
  - automation_pipeline.py
  - scrapers/career_page.py
  - docs/AUTOMATION_IMPROVEMENTS.md (this file)

UNMODIFIED (still work as before):
  - main.py (keyword scraping)
  - company_enricher.py (company enrichment)
  - Google Sheets integration
```

---

## Questions?
Check the logs: `logs/scrape_TIMESTAMP.log` for detailed execution info.
