# Quick Start Guide — New Automation Features

## What's New?

You asked for two things. **Both are now built and ready:**

### ✅ 1. Job Validation Service
Automatically checks if job postings are still active and updates the "Job Status" column in your Jobs sheet.
- **Active**: Job link works
- **Expired**: Link returns error
- **Removed**: Job posting is gone (404)

### ✅ 2. Career Page Scraper
Scrapes **real** job postings from company career pages using your enriched company database.
- Uses the Career Page URLs you've already collected
- Detects the company's ATS (Greenhouse / Lever / Ashby) and pulls clean,
  structured jobs from its public JSON API — real titles + locations, not junk
- Falls back to HTML extraction (listing-id links only) when no ATS is detected
- Note: career pages that render purely via JavaScript and don't expose an ATS
  token won't yield jobs (that's a known limit of static scraping)

### ✅ BONUS: Master Automation Pipeline
Runs everything in the right order:
```
Enrich Companies → Scrape Career Pages → Scrape Keywords → Validate Jobs → Save Results
```

It is **schema-aware**: it adapts to your sheet's actual columns (e.g. the
`Application Platform` / `Sourced By` layout) and never overwrites your header.

---

## 5-Minute Setup

### Step 1: Run the Pipeline
The `Job Status` column is **created automatically** by the validator — no manual
setup needed.

```bash
cd /path/to/automated-scraping
python automation_pipeline.py --config config.json
```

That's it! Watch the logs scroll by as it:
- ✓ Enriches your company database
- ✓ Scrapes career pages (using your company data)
- ✓ Scrapes job keywords
- ✓ Validates all jobs (auto-adds the `Job Status` column)
- ✓ Saves to Google Sheets

### Test on a scratch sheet first
Point `google_sheets.worksheet` at `Jobs_Test` in `config.json` and run just the
validation step:
```bash
python automation_pipeline.py --config config.json --only-validation
```

---

## What Happens in 15 Minutes

**Before:**
```
Companies sheet: 50 companies (name only)
Jobs sheet: ~200 jobs (no status)
```

**After Running Pipeline:**
```
Companies sheet: 50 companies (enriched with employee count, career pages, LinkedIn URLs)
Jobs sheet: ~300+ jobs (with status: Active/Expired/Removed)
```

---

## Common Commands

### Run Everything
```bash
python automation_pipeline.py --config config.json
```

### Run Just Validation (Fast Check)
```bash
python automation_pipeline.py --config config.json --skip-enrichment --skip-career-scraping --skip-keyword-scraping
```

### Run Without Keyword Scraping (Just Career Pages)
```bash
python automation_pipeline.py --config config.json --skip-keyword-scraping
```

### Run Without Career Scraping (Just Keywords)
```bash
python automation_pipeline.py --config config.json --skip-career-scraping
```

---

## Scheduling (Set & Forget)

### Option A: Linux Cron (Free)
Add to your crontab:
```bash
# Daily at 1 AM
0 1 * * * cd /path/to/automated-scraping && python automation_pipeline.py --config config.json
```

### Option B: Google Cloud Scheduler ($)
Create a Cloud Function that calls:
```bash
python automation_pipeline.py --config config.json
```
Schedule for daily at 1 AM.

---

## Expected Results

### Companies Sheet (After Enrichment)
| Company | Employee Count | Career Page | LinkedIn URL |
|---------|---|---|---|
| Stripe | 8,000+ | https://stripe.com/careers | https://linkedin.com/company/stripe |
| Google | 180,000+ | https://careers.google.com | https://linkedin.com/company/google |

### Jobs Sheet (After Validation)
| Job Title | Company | Job Link | Job Status |
|---|---|---|---|
| Backend Engineer | Stripe | https://stripe.com/... | Active |
| Frontend Engineer | Google | https://careers.google... | Active |
| Old Job | Facebook | https://old-job-removed.com | Removed |

---

## What to Expect in Logs

Check `logs/scrape_TIMESTAMP.log`:
```
INFO: Company enrichment complete — updated 5 missing career pages
INFO: Scraped 12 jobs from Stripe career page
INFO: Scraped 8 jobs from Google career page
INFO: Keyword scraping: Indeed found 23 jobs, LinkedIn found 18 jobs
INFO: Validation complete — marked 3 jobs as expired
INFO: Pipeline complete. New jobs written to Google Sheets.
```

---

## Troubleshooting

### "No jobs found from career pages"
- Check that your Companies sheet has valid Career Page URLs
- Run enrichment first: `python automation_pipeline.py --config config.json --skip-keyword-scraping --skip-validation`

### "Job Status column not found"
- Add the column manually to your Jobs sheet, name it exactly: `Job Status`
- Re-run validation

### "Google Sheets API quota exceeded"
- Wait 1 hour and retry
- Or run smaller batches: `python automation_pipeline.py --config config.json --skip-enrichment`

---

## Next Level (Optional)

### Automate More
If you want to run this 100% automated:
1. Deploy to a server/cloud function
2. Schedule with cron or Cloud Scheduler
3. Get email alerts on failures

### Customize
- Edit `scrapers/career_page.py` to add custom career page selectors
- Edit `job_validator.py` to change status rules
- Adjust job filtering in `main.py`

---

## Files You're Using

```
✓ job_validator.py         — Validation service
✓ automation_pipeline.py   — Master orchestrator
✓ scrapers/career_page.py  — Career page scraper
✓ company_enricher.py      — Company enrichment (existing)
✓ main.py                  — Keyword scraping (existing)
```

All existing functionality **still works**. These are additions on top.

---

## Questions?
See: `docs/AUTOMATION_IMPROVEMENTS.md` for detailed docs.
Check logs: `logs/scrape_*.log` for what went wrong.

---

**You're all set!** Run `python automation_pipeline.py --config config.json` and watch it work. 🚀
