# Company Enricher Config Guide

This guide explains how `company_enricher.py` behaves with the current `config.json` options.

## Run Commands

- Default config:
```bash
python company_enricher.py
```

- Custom config path:
```bash
python company_enricher.py --config config.json
```

- Override destination worksheet:
```bash
python company_enricher.py --companies-sheet CompaniesTest
```

## Logging Modes

Log level is controlled at the top of `config.json`:

```json
"logging": {
  "level": "info"
}
```

- `info`: current operational logs (default).
- `debug`: value-level tracing for source reads, target checks, and target replacements.

Use debug when you want to see exactly what each row read/write did in source and destination sheets.

## What The Script Updates

Destination is controlled by:
- `google_sheets.companies_worksheet`

Columns are controlled by:
- `google_sheets.companies_columns.company`
- `google_sheets.companies_columns.employee_count`
- `google_sheets.companies_columns.career_page`
- `google_sheets.companies_columns.linkedin_url`

For each row with a company name, it tries to fill:
- Employee Count
- Career Page
- LinkedIn URL (if missing/invalid)

If data is not found:
- Writes `NA`
- Colors cell red

If data is later found:
- Writes value
- Colors cell white

## IMPORTANT: Source Sheet On vs Off

Source controls are under:
- `google_sheets.source_sheet`

### `source_sheet.enabled = false`

Current behavior:
- No source company sync
- No source LinkedIn fallback
- No source career fallback

Meaning:
- It **does not pull new companies** from any source tab when disabled.
- If `CompaniesTest` is empty, script will find no rows to enrich and exit.

### `source_sheet.enabled = true`

Then source features are controlled by:
- `use_for_company_sync`
- `use_for_linkedin_fallback`
- `use_for_career_fallback`

And source tab/header by:
- `worksheet`
- `company_header`
- `employee_count_header`
- `career_page_header`
- `linkedin_url_header`
- `job_link_header`

If company sync is enabled, missing companies are appended into destination before enrichment.

## Module Layout

The code is now split by responsibility so `company_enricher.py` stays focused on orchestration.

- `company_enricher.py`
  - Loads config, connects to Sheets, coordinates validation and enrichment flow.
- `enricher/config.py`
  - Parses feature flags and header names from `config.json`.
- `enricher/normalizers.py`
  - Normalizes and validates LinkedIn URLs, career URLs, and employee count values.
- `enricher/sheets.py`
  - Handles sheet header lookup, column utilities, and required-header checks.
- `enricher/source_sheet.py`
  - Reads mapped LinkedIn URLs, career pages, and employee counts from the source sheet.
- `enricher/linkedin.py`
  - Contains LinkedIn URL validation/probing and slug discovery helpers.
- `enricher/employee.py`
  - Scrapes employee count from LinkedIn.
- `enricher/career.py`
  - Extracts company website data from LinkedIn, including `/about/`, and probes career pages.

This layout makes it safer to change one enrichment rule without reworking the whole pipeline.

## Current Fallback Order (Per Row)

LinkedIn URL resolution order:
1. Existing value in destination LinkedIn column (if valid)
2. Hyperlink on destination company cell
3. Source-sheet mapped LinkedIn (if source enabled + LinkedIn fallback enabled)
4. Slug probing on LinkedIn (`/company/...`)

Career page resolution order:
1. Extract from LinkedIn company/school page
2. Source-sheet career fallback (if source enabled + career fallback enabled)
3. Otherwise unresolved

Employee count source:
1. Scraped from LinkedIn first
2. Source-sheet employee fallback (if source enabled)

Source employee values can be plain numbers or normalized formats such as:
- `79,222`
- `500k`
- `1.2m`
- `500-600`

When `job_link_header` is set, the enricher also tries to extract a LinkedIn company or school URL from the Jobs tab job link before probing LinkedIn directly.

## Three Independent Features

The script has **three independent features** that can be turned on/off separately:

### FEATURE 1: Full Validation (Master Key)
- **Control**: `google_sheets.validation.enabled`
- **When ON (`true`)**: 
  - Script re-checks **ALL existing values** in the destination sheet before enriching
  - Normalizes LinkedIn URLs, verifies their validity, clears invalid ones
  - Normalizes career page URLs, rejects junk values
  - Syncs cell colors (white=valid, red=invalid/missing)
  - Re-populates invalid/missing fields from scratch
  - Logs per-row validation results
- **When OFF (`false`)**:
  - No full re-validation pass
  - Only enriches fields that meet the skip criteria (based on current values + FEATURE 2 logic)
  - **Does NOT trigger** FEATURE 2 automatically

### FEATURE 2: Retry NA Fields (Independent)
- **Control**: `google_sheets.validation.retry_na_fields`
- **When ON (`true`)**:
  - Treats `NA` (in red) as "pending" instead of "done"
  - Script will **retry those fields** to find values for them
  - Can work **with OR without** FEATURE 1 (validation.enabled)
  - When FEATURE 1 is OFF: light NA prep (clears NA LinkedIns so enrichment can fill)
  - When FEATURE 1 is ON: full validation includes retry logic
  - If combined with FEATURE 3 (source enabled), source becomes fallback when enrichment fails
- **When OFF (`false`)**:
  - Treats `NA` (in red) as "done/locked"
  - Script will **skip those fields** (don't try again)
  - Rows with NA fields are marked as complete

### FEATURE 3: Source Fallback (Independent)
- **Control**: `google_sheets.source_sheet.enabled` + sub-toggles
- **When ON (`true`)**:
  - Source sheet acts as **fallback** when primary sources (LinkedIn discovery, website probing) fail
  - If script can't find an employee count via LinkedIn, it can fall back to source employee count
  - If script can't find a career page, it checks source (if `use_for_career_fallback=true`)
  - If script can't find a LinkedIn URL, it checks source (if `use_for_linkedin_fallback=true`)
  - If source has a company not in destination, appends it (if `use_for_company_sync=true`)
- **When OFF (`false`)**:
  - Only primary sources matter (LinkedIn page scraping, website probing, URL probing)
  - Source sheet is completely ignored

## Behavior Matrix

| Validation | Retry NA | Source | Behavior |
|:---:|:---:|:---:|---|
| ON | ON | ON | Full re-check ALL rows. If still missing: try source fallback. NA fields also retried. |
| ON | ON | OFF | Full re-check ALL rows. No source fallback (slower on missing values). |
| ON | OFF | ON | Full re-check ALL rows. Skip NA fields (treat as done). Source available for non-NA misses. |
| ON | OFF | OFF | Full re-check ALL rows. Skip NA fields. No source fallback. |
| OFF | ON | ON | Light NA prep only. Retry NA fields. Use source fallback when enrichment fails. |
| OFF | ON | OFF | Light NA prep only. Retry NA fields. No source fallback. |
| OFF | OFF | ON | Skip all NA rows. Only enrich truly empty fields. Source available. |
| OFF | OFF | OFF | Skip all NA rows. Only enrich truly empty fields. No source. (Legacy mode) |

## Your Scenario: Retry NA with Source as Fallback

When:
- `validation.enabled = false` (don't recheck everything)
- `retry_na_fields = true` (but DO retry NA fields)
- `source_sheet.enabled = true` (and use source as fallback)

Expected behavior:
1. Light NA prep pass: clears NA LinkedIn fields so they can be refilled
2. Enrichment pass: tries to fill NA fields via primary sources (LinkedIn discovery, website probing)
3. If primary sources fail to find a value: **fallback to source sheet**
4. Rows with non-NA data: skipped (treated as done)

This is perfect for: "I turned on source_worksheet, so I can now fill those NA fields that couldn't be resolved before"

## Legacy Scenarios

### Skip ALL NA fields (old behavior)
```json
"validation": {
  "enabled": false,
  "retry_na_fields": false
},
"source_sheet": {
  "enabled": false
}
```
- Don't recheck anything
- Skip rows with NA (treat as done)
- No source fallback
Result: Fast, conservative (only new empty rows enriched)

## Practical Profiles

### Profile A: Conservative (Only enrich truly empty rows)
```json
"source_sheet": {
  "enabled": false
},
"validation": {
  "enabled": false,
  "retry_na_fields": false
},
"enrichment_controls": {
  "retry_invalid_career_values": ["://"]
}
```
- No validation
- Skip all NA rows
- No source fallback
- Only fill rows with empty cells

### Profile B: NA Retry with Source Fallback (Your use case)
```json
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
  "retry_na_fields": true
},
"enrichment_controls": {
  "retry_invalid_career_values": ["://"]
}
```
- No full validation pass
- **Retry NA fields** using source as fallback
- Light and efficient
- Perfect for: "I have source data now, let me fill those NA fields"

### Profile C: Full Robust Pass (Recommended for comprehensive quality)
```json
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
  "enabled": true,
  "retry_na_fields": true
},
"enrichment_controls": {
  "retry_invalid_career_values": ["://", ""]
}
```
- Full validation: re-check ALL rows
- Retry NA fields as part of validation
- Use source as fallback
- Perfect for: "I want complete data quality across the sheet"

### Profile D: Full Validation, No NA Retry (Conservative validation)
```json
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
  "enabled": true,
  "retry_na_fields": false
},
"enrichment_controls": {
  "retry_invalid_career_values": ["://", ""]
}
```
- Full validation: normalize and verify all data
- Skip NA fields (treat as already processed)
- Use source as fallback for non-NA missing values
- Perfect for: "I want to clean existing data but not retry old NA fields"

## Notes

- `FutureWarning` about Python 3.10 is from Google libs and is informational for now.
- Exit code `130` means run was interrupted (Ctrl+C), not completed.
- Validation/enrichment scope log shows only rows where company column has a value.
