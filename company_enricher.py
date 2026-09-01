"""
company_enricher.py — Standalone company enrichment pipeline.

Reads the enrichment output worksheet (config: google_sheets.enrichment_output_worksheet)
and fills in missing data by scraping LinkedIn and company websites:
    - Task 1: fills Employee-Count column (scraped from LinkedIn)
    - Task 2: fills Career-Page column
    - Task 3: syncs unique company names from the Jobs tab into the enrichment sheet

Rules:
    - Row 1 is a header row; data starts from row 2
    - Column/header mapping is configurable via config.yaml (google_sheets.enrichment_output_columns)
    - Only rows where BOTH employee and career columns are empty are processed
    - No duplicate company entries are added from the Jobs tab

Usage:
    python company_enricher.py
    python company_enricher.py --config config.yaml --companies-sheet "CompaniesTest"
"""

import argparse
import logging
import sys
import time
import requests

import projects_registry
from google_sheets_store import sheets_call
from config_loader import load_config

from enricher import config as config_helpers
from enricher import career as career_helpers
from enricher import employee as employee_helpers
from enricher import linkedin as linkedin_helpers
from enricher import normalizers
from enricher import sheets as sheets_helpers
from enricher import source_sheet as source_sheet_pkg
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Patterns to extract employee count from page text
_EMPLOYEE_PATTERNS = [
    r"see all\s*([\d,]+)\s*employees\s*on\s*linkedin",
    r"([\d,]+)\s*associated\s*members",
    r"([\d,]+\+?)\s*employees",
    r"employees[:\s]+([\d,]+\+?)",
    r"([\d,]+\+?)\s*staff",
    r"company size[:\s]+([^\n<]+)",
    r"([\d,]+\s*[-–]\s*[\d,]+)\s*employees",
]

# Common career page path suffixes to probe
_CAREER_PATHS = [
    "/careers",
    "/jobs",
    "/careers/",
    "/jobs/",
    "/work-with-us",
    "/join-us",
    "/join",
    "/about/careers",
]

# Common career subdomains to probe
_CAREER_SUBDOMAINS = ["careers", "jobs", "work"]

# Delay between LinkedIn requests (seconds) — keeps us under rate limits
_LINKEDIN_DELAY = 4

# ── Google Auth ───────────────────────────────────────────────────────────────

def _build_credentials(gs_config: dict):
    from google.oauth2.service_account import Credentials

    creds_file = gs_config.get("credentials_file", "")
    if not creds_file:
        raise RuntimeError("Missing credentials_file in google_sheets config.")
    return Credentials.from_service_account_file(creds_file, scopes=_SCOPES)


def _gspread_client(creds):
    import gspread
    return gspread.authorize(creds)


def _sheets_api(creds):
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheets_call(fn, *args, **kwargs):
    """Retry wrapper for gspread calls — see google_sheets_store.sheets_call.

    Kept as a thin alias because this module calls it in many places. The
    implementation is shared so the two copies that used to live here and in
    cleanup_validation.py cannot drift apart again.
    """
    return sheets_call(fn, *args, **kwargs)


def _set_bg(comp_ws, cell_ref: str, rgb: dict) -> None:
    _sheets_call(comp_ws.format, cell_ref, {"backgroundColor": rgb})
# ── Main enrichment pipeline ──────────────────────────────────────────────────

def enrich(gs_config: dict, companies_sheet_name: str) -> None:
    import gspread

    spreadsheet_id = gs_config.get("spreadsheet_id", "")
    source_cfg = config_helpers.source_sheet_controls(gs_config)
    source_sheet_name = source_cfg["worksheet"]
    source_company_header = source_cfg["company_header"]
    source_employee_header = source_cfg["employee_count_header"]
    source_career_header = source_cfg["career_page_header"]
    source_linkedin_header = source_cfg["linkedin_url_header"]
    source_job_link_header = source_cfg["job_link_header"]

    creds = _build_credentials(gs_config)
    gc = _gspread_client(creds)
    sheets_svc = _sheets_api(creds)

    # Open spreadsheet
    try:
        spreadsheet = gc.open_by_key(spreadsheet_id)
    except PermissionError as exc:
        svc_email = getattr(creds, "service_account_email", "<service-account-email>")
        raise RuntimeError(
            f"Google Sheets access denied (403). Share the spreadsheet with: {svc_email}"
        ) from exc

    # Get or create the Companies worksheet
    try:
        comp_ws = spreadsheet.worksheet(companies_sheet_name)
        logger.info(f"Found worksheet '{companies_sheet_name}'")
    except gspread.WorksheetNotFound:
        comp_ws = spreadsheet.add_worksheet(
            title=companies_sheet_name, rows=1000, cols=10
        )
        logger.info(f"Created new worksheet '{companies_sheet_name}'")

    # Ensure required headers exist and capture their actual column indexes.
    required_headers = config_helpers.required_company_columns(gs_config)
    col_idx = sheets_helpers.ensure_required_headers(comp_ws, required_headers, _sheets_call)
    controls = config_helpers.enrichment_controls(gs_config)
    validation_controls = config_helpers.validation_controls(gs_config)
    retry_na_fields = validation_controls["retry_na_fields"]
    retry_invalid_career_values = controls["retry_invalid_career_values"]
    validate_enabled = validation_controls["enabled"]
    retry_na_when_validation_off = not validate_enabled and retry_na_fields
    
    if validate_enabled:
        logger.info("Validation mode enabled: re-checking ALL existing values before enrichment")
    elif retry_na_when_validation_off:
        logger.info("Validation mode disabled: but retry_na_fields enabled — will retry NA (red) fields only")
    else:
        logger.info("Validation mode disabled and retry_na_fields disabled: only enriching truly empty fields")
    
    if source_cfg["enabled"]:
        logger.info("Source fallback enabled: will use source data when primary sources fail to find values")
    else:
        logger.info("Source fallback disabled: only primary sources (LinkedIn discovery, website probing)")

    # ── Task 3: sync new companies from Jobs tab ──────────────────────────────
    # Use Sheets API v4 so we capture the LinkedIn hyperlinks on Company cells
    jobs_company_data = {}
    source_career_data = {}
    source_employee_data = {}
    if source_cfg["enabled"]:
        logger.info(
            "Source headers: "
            f"company='{source_company_header}', "
            f"employee='{source_employee_header}', "
            f"career='{source_career_header}', "
            f"linkedin='{source_linkedin_header}', "
            f"job_link='{source_job_link_header}'"
        )
        jobs_company_data = source_sheet_pkg.get_jobs_company_linkedin(
            sheets_svc,
            spreadsheet_id,
            source_sheet_name,
            company_header=source_company_header,
            linkedin_header=source_linkedin_header,
            job_link_header=source_job_link_header,
        )
        logger.info(
            f"Source tab '{source_sheet_name}' ({source_company_header}): "
            f"{len(jobs_company_data)} unique companies"
        )

        if source_cfg["use_for_career_fallback"]:
            source_career_data = source_sheet_pkg.get_source_sheet_career_pages(
                sheets_svc,
                spreadsheet_id,
                source_sheet_name,
                company_header=source_company_header,
                career_page_header=source_career_header,
            )
            logger.info(
                f"Source tab career pages available for {len(source_career_data)} companies"
            )

        source_employee_data = source_sheet_pkg.get_source_sheet_employee_counts(
            sheets_svc,
            spreadsheet_id,
            source_sheet_name,
            company_header=source_company_header,
            employee_count_header=source_employee_header,
        )
        logger.info(
            f"Source tab employee counts available for {len(source_employee_data)} companies"
        )
        logger.debug("Source LinkedIn sample: %s", list(jobs_company_data.items())[:5])
        logger.debug("Source career sample: %s", list(source_career_data.items())[:5])
        logger.debug("Source employee sample: %s", list(source_employee_data.items())[:5])
    else:
        logger.info("Source sheet usage disabled by config")

    # Read all values in Companies tab (including header)
    all_values = comp_ws.get_all_values()
    company_i = col_idx["company"]
    employee_i = col_idx["employee_count"]
    career_i = col_idx["career_page"]
    linkedin_i = col_idx["linkedin_url"]

    existing_names = {
        (row[company_i].strip().lower() if len(row) > company_i else "")
        for row in all_values[1:]
        if row and len(row) > company_i and row[company_i].strip()
    }

    # Append only companies not already present
    new_companies = []
    if source_cfg["enabled"] and source_cfg["use_for_company_sync"]:
        new_companies = sorted(
            [
                name
                for name in jobs_company_data.keys()
                if name.strip().lower() not in existing_names
            ]
        )
    if new_companies:
        rows_to_add = []
        max_i = max(company_i, employee_i, career_i, linkedin_i)
        for name in new_companies:
            linkedin_url = jobs_company_data.get(name, "")
            row = [""] * (max_i + 1)
            row[company_i] = name
            if linkedin_url:
                row[linkedin_i] = linkedin_url
            rows_to_add.append(row)
        _sheets_call(comp_ws.append_rows, rows_to_add, value_input_option="USER_ENTERED")
        logger.info(
            f"Added {len(new_companies)} new companies from source tab '{source_sheet_name}'"
        )
    else:
        if source_cfg["enabled"] and source_cfg["use_for_company_sync"]:
            logger.info(
                f"No new companies to add from source tab '{source_sheet_name}'"
            )
        else:
            logger.info("Company sync from source sheet disabled by config")

    # ── Tasks 1 & 2: enrich employee count and career page ────────────────────
    # Re-fetch company column values with hyperlink metadata after appends.
    cell_data = sheets_helpers.get_column_data(
        sheets_svc,
        spreadsheet_id,
        companies_sheet_name,
        company_i,
        start_row=2,
    )
    if not cell_data:
        logger.warning("No data found in Companies tab company column — nothing to enrich")
        return

    row_keys = sorted(cell_data.keys())
    logger.info(
        f"Validation/enrichment scope: {len(row_keys)} company rows "
        f"(first_row={row_keys[0]}, last_row={row_keys[-1]})"
    )

    # Re-read current B and C values
    all_values = comp_ws.get_all_values()

    enriched_count = 0
    skipped_count = 0
    validated_rows = 0
    validation_updates = 0
    validation_color_changes = 0
    validation_linkedin_valid = 0
    validation_linkedin_invalid = 0
    validation_linkedin_unknown = 0
    validation_linkedin_missing = 0
    validation_employee_valid = 0
    validation_employee_invalid = 0
    validation_career_valid = 0
    validation_career_invalid = 0

    for row_num, info in sorted(cell_data.items()):
        company_name = info["value"]

        # Fetch current B, C, D values for this row
        row_idx = row_num - 1  # 0-indexed
        if row_idx < len(all_values):
            row_vals = all_values[row_idx]
            current_b = row_vals[employee_i].strip() if len(row_vals) > employee_i else ""
            current_c = row_vals[career_i].strip() if len(row_vals) > career_i else ""
            current_d = row_vals[linkedin_i].strip() if len(row_vals) > linkedin_i else ""
        else:
            current_b, current_c, current_d = "", "", ""

        logger.debug(
            "Row %s '%s' current target values | employee='%s' career='%s' linkedin='%s'",
            row_num,
            company_name,
            current_b,
            current_c,
            current_d,
        )

        b_col = sheets_helpers.col_index_to_letter(employee_i)
        c_col = sheets_helpers.col_index_to_letter(career_i)
        d_col = sheets_helpers.col_index_to_letter(linkedin_i)

        if validate_enabled:
            # FEATURE 1: FULL VALIDATION (validation.enabled=true)
            # Re-check ALL existing values, normalize, verify, sync colors
            validated_rows += 1
            row_validation_notes = []
            li_status = "missing"
            invalid_linkedin_cleared = False

            normalized_d = normalizers.normalize_linkedin_url(current_d)
            if current_d and normalized_d and normalized_d != current_d:
                logger.debug(
                    "Row %s '%s' normalize linkedin target value '%s' -> '%s'",
                    row_num,
                    company_name,
                    current_d,
                    normalized_d,
                )
                _sheets_call(
                    comp_ws.update,
                    [[normalized_d]],
                    f"{d_col}{row_num}",
                    value_input_option="USER_ENTERED",
                )
                current_d = normalized_d
                validation_updates += 1
                row_validation_notes.append("linkedin_normalized")

            if current_d:
                li_status = linkedin_helpers.linkedin_profile_status(current_d)
                if li_status == "invalid":
                    # Clear invalid stored URL so normal resolution path can refill it.
                    logger.debug(
                        "Row %s '%s' clearing invalid linkedin value '%s'",
                        row_num,
                        company_name,
                        current_d,
                    )
                    _sheets_call(
                        comp_ws.update,
                        [[""]],
                        f"{d_col}{row_num}",
                        value_input_option="USER_ENTERED",
                    )
                    current_d = ""
                    invalid_linkedin_cleared = True
                    validation_updates += 1
                    row_validation_notes.append("linkedin_cleared_invalid")
            if not current_d and li_status != "invalid":
                li_status = "missing"
            normalized_c = normalizers.normalize_career_page(current_c)
            if current_c and normalized_c and normalized_c != current_c:
                logger.debug(
                    "Row %s '%s' normalize career target value '%s' -> '%s'",
                    row_num,
                    company_name,
                    current_c,
                    normalized_c,
                )
                _sheets_call(
                    comp_ws.update,
                    [[normalized_c]],
                    f"{c_col}{row_num}",
                    value_input_option="USER_ENTERED",
                )
                current_c = normalized_c
                validation_updates += 1
                row_validation_notes.append("career_normalized")

            # Sync visual state while validating existing data.
            _set_bg(
                comp_ws,
                f"{d_col}{row_num}",
                {"red": 1.0, "green": 1.0, "blue": 1.0}
                if normalizers.is_valid_linkedin_url(current_d)
                else {"red": 1.0, "green": 0.0, "blue": 0.0},
            )
            _set_bg(
                comp_ws,
                f"{b_col}{row_num}",
                {"red": 1.0, "green": 1.0, "blue": 1.0}
                if normalizers.is_numeric_employee_count(current_b)
                else {"red": 1.0, "green": 0.0, "blue": 0.0},
            )
            career_valid_now = bool(
                normalizers.normalize_career_page(current_c)
                and not normalizers.is_invalid_career_value(current_c, retry_invalid_career_values)
                and not normalizers.is_na(current_c)
            )
            _set_bg(
                comp_ws,
                f"{c_col}{row_num}",
                {"red": 1.0, "green": 1.0, "blue": 1.0}
                if career_valid_now
                else {"red": 1.0, "green": 0.0, "blue": 0.0},
            )
            validation_color_changes += 3

            employee_valid_now = bool(normalizers.is_numeric_employee_count(current_b))

            if li_status == "valid":
                validation_linkedin_valid += 1
            elif li_status == "invalid":
                validation_linkedin_invalid += 1
            elif li_status == "unknown":
                validation_linkedin_unknown += 1
            else:
                validation_linkedin_missing += 1

            if employee_valid_now:
                validation_employee_valid += 1
            else:
                validation_employee_invalid += 1

            if career_valid_now:
                validation_career_valid += 1
            else:
                validation_career_invalid += 1

            logger.info(
                f"Validation check row {row_num} '{company_name}': "
                f"linkedin={li_status}, "
                f"employee={'valid' if employee_valid_now else 'invalid_or_missing'}, "
                f"career={'valid' if career_valid_now else 'invalid_or_missing'}"
            )

            if row_validation_notes:
                logger.info(
                    f"Validation update row {row_num} '{company_name}': "
                    + ", ".join(row_validation_notes)
                )
        elif retry_na_when_validation_off:
            # FEATURE 2: LIGHT NA RETRY (validation.enabled=false but retry_na_fields=true)
            # Only check NA fields to mark them for retry by enrichment
            validated_rows += 1
            invalid_linkedin_cleared = False
            row_validation_notes = []
            
            if normalizers.is_na(current_d):
                # Clear NA LinkedIn so enrichment can refill it
                logger.debug(
                    "Row %s '%s' clearing NA linkedin in target for retry",
                    row_num,
                    company_name,
                )
                _sheets_call(
                    comp_ws.update,
                    [[""]],
                    f"{d_col}{row_num}",
                    value_input_option="USER_ENTERED",
                )
                current_d = ""
                invalid_linkedin_cleared = True
                validation_updates += 1
                row_validation_notes.append("linkedin_cleared_na_for_retry")
            
            if row_validation_notes:
                logger.debug(
                    f"NA retry row {row_num} '{company_name}': "
                    + ", ".join(row_validation_notes)
                )
        else:
            # No full validation and no NA retry (legacy behavior)
            invalid_linkedin_cleared = False

        employee_done = (
            bool(current_b)
            and (
                normalizers.is_numeric_employee_count(current_b)
                or (normalizers.is_na(current_b) and not retry_na_fields)
            )
        )
        career_done = (
            bool(current_c)
            and (
                (
                    bool(normalizers.normalize_career_page(current_c))
                    and not normalizers.is_invalid_career_value(current_c, retry_invalid_career_values)
                )
                or (normalizers.is_na(current_c) and not retry_na_fields)
            )
        )
        linkedin_locked = normalizers.is_na(current_d) and not retry_na_fields

        need_employee = not employee_done
        need_career = not career_done
        force_linkedin_refresh = invalid_linkedin_cleared

        # Skip only if both target fields are already satisfied, or if LinkedIn
        # was previously marked NA and NA-retry mode is disabled.
        if ((not need_employee and not need_career) and not force_linkedin_refresh) or linkedin_locked:
            skipped_count += 1
            if validate_enabled:
                logger.info(
                    f"Validation checked row {row_num} '{company_name}' — "
                    f"no enrichment needed (employee_done={employee_done}, "
                    f"career_done={career_done}, linkedin_locked={linkedin_locked})"
                )
            logger.debug(
                f"Row {row_num} '{company_name}' — skipped (already has data: "
                f"B='{current_b}' C='{current_c}' D='{current_d}')"
            )
            continue

        # Resolve LinkedIn URL: configured LinkedIn column (skip if "NA")
        # > company-cell hyperlink > Jobs tab > slug probe
        # Validate each candidate — reject malformed URLs (e.g. http://ompany/...)
        def _valid_linkedin(u: str) -> str:
            return normalizers.normalize_linkedin_url(u)

        linkedin_url = (
            _valid_linkedin(current_d if not normalizers.is_na(current_d) else "")
            or _valid_linkedin(info["url"])
            or (
                _valid_linkedin(jobs_company_data.get(company_name, ""))
                if source_cfg["enabled"] and source_cfg["use_for_linkedin_fallback"]
                else ""
            )
        )
        logger.debug(
            "Row %s '%s' linkedin resolution inputs | target='%s' company_hyperlink='%s' source='%s' resolved='%s'",
            row_num,
            company_name,
            current_d,
            info["url"],
            jobs_company_data.get(company_name, "") if source_cfg["enabled"] else "",
            linkedin_url,
        )

        # Normalize: strip any path beyond /company/<slug>/ or /school/<slug>/
        if linkedin_url:
            linkedin_url = normalizers.normalize_linkedin_url(linkedin_url)

        if not linkedin_url:
            logger.info(f"Row {row_num} '{company_name}' — probing LinkedIn for URL")
            linkedin_url = linkedin_helpers.find_linkedin_url(company_name)

        if not linkedin_url:
            # Write NA so future runs skip this row without re-probing
            linkedin_col = sheets_helpers.col_index_to_letter(linkedin_i)
            logger.debug(
                "Row %s '%s' writing target linkedin NA at %s%s",
                row_num,
                company_name,
                linkedin_col,
                row_num,
            )
            _sheets_call(
                comp_ws.update,
                [["NA"]],
                f"{linkedin_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
            _sheets_call(
                comp_ws.format,
                f"{linkedin_col}{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
            logger.info(
                f"Row {row_num} '{company_name}' — no LinkedIn URL found, wrote NA"
            )
            skipped_count += 1
            continue

        # Write LinkedIn URL to configured LinkedIn column if not already there
        if not current_d or normalizers.is_na(current_d):
            linkedin_col = sheets_helpers.col_index_to_letter(linkedin_i)
            logger.debug(
                "Row %s '%s' writing target linkedin '%s' at %s%s (previous='%s')",
                row_num,
                company_name,
                linkedin_url,
                linkedin_col,
                row_num,
                current_d,
            )
            _sheets_call(
                comp_ws.update,
                [[linkedin_url]],
                f"{linkedin_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
            # Clear previous NA highlight when LinkedIn URL is now resolved.
            _sheets_call(
                comp_ws.format,
                f"{linkedin_col}{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            )
            logger.info(f"Row {row_num} '{company_name}' — LinkedIn URL written: {linkedin_url}")

        logger.info(f"Row {row_num} | '{company_name}' | {linkedin_url}")

        # Pre-fetch company website once — shared by employee and career scrapers
        # to avoid fetching the LinkedIn page twice for the same row.
        company_website = career_helpers.get_company_website(linkedin_url) if linkedin_url else ""
        if company_website:
            logger.debug(
                "Row %s '%s' company website resolved: '%s'",
                row_num,
                company_name,
                company_website,
            )

        # Task 1: employee count (LinkedIn first, website fallback, source fallback)
        employee_count = ""
        if need_employee:
            employee_raw = employee_helpers.scrape_employee_count(
                linkedin_url, website=company_website, company_name=company_name,
            )
            employee_count = normalizers.normalize_employee_count(employee_raw)
            if not employee_count and source_cfg["enabled"]:
                source_employee = source_employee_data.get(company_name, "")
                employee_count = normalizers.normalize_source_employee_count(source_employee)
                logger.debug(
                    "Row %s '%s' employee source fallback | source_raw='%s' normalized='%s'",
                    row_num,
                    company_name,
                    source_employee,
                    employee_count,
                )
                if employee_count:
                    logger.debug(
                        f"  employee_count (from source): '{employee_count}'"
                    )
            logger.info(
                f"  employee_count_raw: '{employee_raw}' | employee_count: '{employee_count}'"
            )
            time.sleep(_LINKEDIN_DELAY)

        # Task 2: career page (pass pre-fetched website to avoid duplicate LinkedIn fetch)
        career_page = ""
        if need_career:
            career_page = career_helpers.find_career_page(company_name, linkedin_url, website=company_website)
            # Fallback to source sheet career page if not found via LinkedIn enrichment
            if not career_page:
                career_page = (
                    source_career_data.get(company_name, "")
                    if source_cfg["enabled"] and source_cfg["use_for_career_fallback"]
                    else ""
                )
                if career_page:
                    career_page = normalizers.normalize_career_page(career_page)
                    logger.debug(
                        "Row %s '%s' career source fallback normalized='%s'",
                        row_num,
                        company_name,
                        career_page,
                    )
                    logger.debug(f"  career_page (from source): {career_page}")
            logger.info(f"  career_page: '{career_page}'")
            time.sleep(_LINKEDIN_DELAY)

        # Write only pending fields — use "NA" when a value could not be found
        if need_employee:
            out_b = employee_count if employee_count else "NA"
            logger.debug(
                "Row %s '%s' writing target employee '%s' at %s%s (previous='%s')",
                row_num,
                company_name,
                out_b,
                b_col,
                row_num,
                current_b,
            )
            _sheets_call(
                comp_ws.update,
                [[out_b]],
                f"{b_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
            if employee_count:
                # Clear previous NA highlight when employee count is now resolved.
                _sheets_call(
                    comp_ws.format,
                    f"{b_col}{row_num}",
                    {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                )
        if need_career:
            out_c = career_page if career_page else "NA"
            logger.debug(
                "Row %s '%s' writing target career '%s' at %s%s (previous='%s')",
                row_num,
                company_name,
                out_c,
                c_col,
                row_num,
                current_c,
            )
            _sheets_call(
                comp_ws.update,
                [[out_c]],
                f"{c_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
            if career_page:
                # Clear previous NA highlight when career page is now resolved.
                _sheets_call(
                    comp_ws.format,
                    f"{c_col}{row_num}",
                    {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                )

        # Color cells red where we wrote NA (not found)
        if need_employee and not employee_count:
            _sheets_call(
                comp_ws.format,
                f"{b_col}{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
        if need_career and not career_page:
            _sheets_call(
                comp_ws.format,
                f"{c_col}{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
        enriched_count += 1
        time.sleep(1)  # brief pause between sheet writes

    logger.info(
        f"Done. {enriched_count} rows enriched, {skipped_count} rows skipped (already had data)."
    )
    if validate_enabled:
        logger.info(
            "Validation summary: "
            f"rows_checked={validated_rows}, "
            f"value_updates={validation_updates}, "
            f"color_updates={validation_color_changes}, "
            f"linkedin(valid={validation_linkedin_valid},invalid={validation_linkedin_invalid},"
            f"unknown={validation_linkedin_unknown},missing={validation_linkedin_missing}), "
            f"employee(valid={validation_employee_valid},invalid_or_missing={validation_employee_invalid}), "
            f"career(valid={validation_career_valid},invalid_or_missing={validation_career_invalid})"
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich company data (employee count + career page) in Google Sheets"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml or config.json (default: config.yaml)",
    )
    parser.add_argument(
        "--companies-sheet",
        default=None,
        dest="companies_sheet",
        help="Enrichment output worksheet name (overrides config.yaml google_sheets.enrichment_output_worksheet)",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config, name="enrich")
    logger.info(
        "Logger initialized from config | level=%s",
        logging.getLevelName(logging.getLogger().level),
    )

    gs_config = config.get("google_sheets", {})
    if not gs_config.get("enabled", False):
        logger.error(
            "Google Sheets is not enabled in config.json. "
            "Set google_sheets.enabled = true and provide credentials."
        )
        sys.exit(1)

    # CLI arg overrides config; config overrides default
    companies_sheet = (
        args.companies_sheet
        or gs_config.get("enrichment_output_worksheet", "CompaniesTest")
    )
    enrich(gs_config, companies_sheet)


if __name__ == "__main__":
    main()
