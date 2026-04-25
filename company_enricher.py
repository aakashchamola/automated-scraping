"""
company_enricher.py — Standalone company enrichment pipeline.

Reads a "Companies" tab in your Google Sheet and:
  - Task 1: fills Column B with employee count (scraped from LinkedIn)
  - Task 2: fills Column C with the company's career page URL
  - Task 3: syncs unique company names from the Jobs tab into the Companies tab

Rules:
  - Row 1 is a header row; data starts from row 2
  - Column A: company name (cells may be hyperlinked to a LinkedIn company page)
  - Only rows where BOTH Column B and Column C are empty are processed
  - No duplicate company entries are added from the Jobs tab

Usage:
    python company_enricher.py
    python company_enricher.py --config config.json --companies-sheet "Companies"
"""

import argparse
import json
import logging
import re
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from logger_setup import setup_logging

setup_logging()
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


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.error(f"Config file not found: '{path}'")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error(f"Config JSON is invalid: {exc}")
        sys.exit(1)


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


def _sheets_call(fn, *args, retries: int = 4, backoff: float = 8.0, **kwargs):
    """Call a gspread method with retry on transient network / API errors."""
    import gspread.exceptions

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_exc = exc
            wait = backoff * (2 ** attempt)
            logger.warning(
                f"Sheets network error (attempt {attempt + 1}/{retries}), "
                f"retrying in {wait:.0f}s: {exc}"
            )
            time.sleep(wait)
        except gspread.exceptions.APIError as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in (429, 500, 502, 503, 504):
                last_exc = exc
                wait = backoff * (2 ** attempt)
                logger.warning(
                    f"Sheets API error {status} (attempt {attempt + 1}/{retries}), "
                    f"retrying in {wait:.0f}s"
                )
                time.sleep(wait)
            else:
                raise
    raise last_exc


# ── Column-letter helper ─────────────────────────────────────────────────────

def _col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to A1-notation letter (A, B, ..., AA, ...)."""
    result = ""
    n = idx + 1  # 1-based
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


# ── Hyperlink extraction via Sheets API v4 ───────────────────────────────────

def get_column_a_data(sheets_svc, spreadsheet_id: str, sheet_name: str) -> dict:
    """
    Returns a dict mapping 1-based row number -> {"name": str, "url": str}
    for every non-empty cell in column A of sheet_name (starting at row 2).

    The "url" is the hyperlink attached to the cell (empty string if none).
    Uses the Sheets API v4 includeGridData to access raw hyperlink metadata,
    which works for both formula-based and UI-added hyperlinks.
    """
    range_notation = f"'{sheet_name}'!A2:A"
    try:
        result = (
            sheets_svc.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[range_notation],
                includeGridData=True,
                fields=(
                    "sheets.data.rowData.values.hyperlink,"
                    "sheets.data.rowData.values.formattedValue"
                ),
            )
            .execute()
        )
    except Exception as exc:
        logger.error(f"Sheets API call failed: {exc}")
        return {}

    rows = (
        result.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    data = {}
    for i, row in enumerate(rows):
        actual_row = i + 2  # data starts at row 2
        cells = row.get("values", [])
        if not cells:
            continue
        cell = cells[0]
        name = cell.get("formattedValue", "").strip()
        url = cell.get("hyperlink", "").strip()
        if name:
            data[actual_row] = {"name": name, "url": url}

    return data


def get_jobs_company_linkedin(sheets_svc, spreadsheet_id: str, sheet_name: str) -> dict:
    """
    Read the 'Company' column from the Jobs tab using the Sheets API v4,
    returning a dict mapping company_name -> linkedin_url ("" when no hyperlink).
    Only the first occurrence of each company name is kept.
    """
    # Find which column index holds 'Company'
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        header_resp = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!1:1")
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Could not read Jobs tab header: {exc}")
        return {}

    header_row = header_resp.get("values", [[]])
    header_row = header_row[0] if header_row else []
    try:
        col_idx = header_row.index("Company")
    except ValueError:
        logger.warning(f"No 'Company' column found in '{sheet_name}' header")
        return {}

    col_letter = _col_index_to_letter(col_idx)
    range_notation = f"'{sheet_name}'!{col_letter}2:{col_letter}"

    try:
        result = (
            sheets_svc.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[range_notation],
                includeGridData=True,
                fields=(
                    "sheets.data.rowData.values.hyperlink,"
                    "sheets.data.rowData.values.formattedValue"
                ),
            )
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Sheets API call failed reading Jobs company column: {exc}")
        return {}

    rows = (
        result.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    data = {}
    for row in rows:
        cells = row.get("values", [])
        if not cells:
            continue
        cell = cells[0]
        name = cell.get("formattedValue", "").strip()
        url = cell.get("hyperlink", "").strip()
        if name and name not in data:
            data[name] = url

    return data


# ── LinkedIn scraping ─────────────────────────────────────────────────────────

def scrape_employee_count(linkedin_url: str) -> str:
    """
    Scrape the employee count from a LinkedIn company /about page.
    Returns a string like "1,001-5,000 employees" or "" if not found.
    LinkedIn aggressively rate-limits; gracefully return "" on failure.
    """
    if not linkedin_url or "linkedin.com" not in linkedin_url:
        return ""

    # Use the base company page — LinkedIn redirects /about to login,
    # but the base page serves employee data without authentication.
    url = linkedin_url.rstrip("/") + "/"

    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=12, allow_redirects=True)
    except requests.RequestException as exc:
        logger.warning(f"LinkedIn request failed for {linkedin_url}: {exc}")
        return ""

    if resp.status_code == 999:
        logger.warning(f"LinkedIn rate-limited (999) for {linkedin_url}")
        return ""
    if resp.status_code != 200:
        logger.warning(f"LinkedIn returned HTTP {resp.status_code} for {linkedin_url}")
        return ""
    # If redirected to login, we cannot extract data
    if "login" in resp.url or "authwall" in resp.url or "/company/" not in resp.url:
        logger.warning(f"LinkedIn redirected to login/authwall for {linkedin_url}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1. Try JSON-LD structured data (most reliable when present)
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                emp = data.get("numberOfEmployees")
                if isinstance(emp, dict):
                    val = emp.get("value")
                    if val:
                        return f"{val} employees"
                    min_v = emp.get("minValue")
                    max_v = emp.get("maxValue")
                    if min_v and max_v:
                        return f"{min_v}-{max_v} employees"
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. Scan visible text with regex patterns
    text = soup.get_text(" ", strip=True)
    for pattern in _EMPLOYEE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return ""


def get_company_website(linkedin_url: str) -> str:
    """
    Try to extract the company's own website URL from their LinkedIn /about page.
    """
    if not linkedin_url or "linkedin.com" not in linkedin_url:
        return ""

    # Use the base company page (same reason as scrape_employee_count — /about redirects to login)
    url = linkedin_url.rstrip("/") + "/"

    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=12, allow_redirects=True)
    except requests.RequestException:
        return ""

    if resp.status_code != 200:
        return ""
    if "login" in resp.url or "authwall" in resp.url or "/company/" not in resp.url:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # JSON-LD structured data only — anchor scanning picks up too many stray links
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            # LinkedIn wraps company data in @graph; find the Organization node
            nodes = data.get("@graph", []) if isinstance(data, dict) else []
            if not nodes and isinstance(data, dict):
                nodes = [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                # sameAs = the company's external website
                same_as = node.get("sameAs", "")
                if isinstance(same_as, list):
                    # Prefer root domains over subdomains/deep paths
                    candidates = [
                        s for s in same_as
                        if isinstance(s, str) and "linkedin.com" not in s and s.startswith("http")
                    ]
                    def _url_score(u: str) -> tuple:
                        from urllib.parse import urlparse as _up2
                        p = _up2(u)
                        subdomain_depth = p.netloc.count(".") - 1  # more dots = deeper subdomain
                        path_depth = len([x for x in p.path.split("/") if x])
                        return (subdomain_depth, path_depth)
                    candidates.sort(key=_url_score)
                    same_as = candidates[0] if candidates else ""
                if same_as and "linkedin.com" not in same_as:
                    return same_as
                # Fallback: url field when it's not LinkedIn's own URL
                site = node.get("url", "")
                if site and "linkedin.com" not in site:
                    return site
        except (json.JSONDecodeError, AttributeError):
            pass

    return ""


# ── LinkedIn URL discovery ────────────────────────────────────────────────────


def _linkedin_slug_candidates(company_name: str) -> list:
    """Generate potential LinkedIn company URL slugs from a plain company name."""
    import unicodedata

    name = unicodedata.normalize("NFKD", company_name)
    name = name.encode("ascii", "ignore").decode("ascii")

    def to_slug(n: str) -> str:
        n = re.sub(r"\s*&\s*", " and ", n)
        n = n.lower()
        n = re.sub(r"[^a-z0-9\s-]", "", n)
        n = re.sub(r"\s+", "-", n.strip())
        n = re.sub(r"-+", "-", n).strip("-")
        return n

    seen = []
    slug_full = to_slug(name)
    if slug_full:
        seen.append(slug_full)

    # Try without common legal suffixes
    name_stripped = re.sub(
        r"\s*\b(llc|inc\.?|corp\.?|ltd\.?|co\.?|plc|gmbh|ag|sa|bv|nv|lp|llp|pvt\.?|limited|incorporated|corporation)\b",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    slug_stripped = to_slug(name_stripped)
    if slug_stripped and slug_stripped not in seen:
        seen.append(slug_stripped)

    return seen


def find_linkedin_url(company_name: str) -> str:
    """
    Discover a company's LinkedIn URL by probing likely URL slugs directly on
    linkedin.com.  No external search engine required.

    Signals:
      - HTTP 999  -> rate-limited, but the company page exists at this slug
      - HTTP 200 with /company/ still in the final URL -> valid page
      - HTTP 404 / redirect to /search/ or authwall -> wrong slug, try next
    """
    slugs = _linkedin_slug_candidates(company_name)
    for slug in slugs:
        url = f"https://www.linkedin.com/company/{slug}/"
        try:
            resp = requests.get(
                url, headers=_BROWSER_HEADERS, timeout=10, allow_redirects=True
            )
            if resp.status_code == 999:
                # LinkedIn blocks bots on valid pages — slug is correct
                logger.debug(f"LinkedIn slug probe 999 (valid): {url}")
                return url
            if resp.status_code == 200:
                final = resp.url
                if "/company/" in final and "/search/" not in final and "authwall" not in final:
                    return url
                # LinkedIn sometimes redirects valid pages to the authwall.
                # The authwall URL carries the original target in referrerUrl.
                if "authwall" in final:
                    from urllib.parse import parse_qs, unquote, urlparse as _up
                    qs = parse_qs(_up(final).query)
                    ref = unquote(qs.get("referrerUrl", [""])[0])
                    if f"/company/{slug}" in ref:
                        return url
            # 404 or redirect to /search/ = wrong slug, try next
        except requests.RequestException as exc:
            logger.debug(f"LinkedIn probe failed for {url}: {exc}")
        time.sleep(1)
    return ""


# ── Career page discovery ─────────────────────────────────────────────────────

def _probe_url(url: str, timeout: int = 8) -> bool:
    """Return True if the URL responds with a non-4xx/5xx status."""
    try:
        resp = requests.head(
            url, headers=_BROWSER_HEADERS, timeout=timeout, allow_redirects=True
        )
        return resp.status_code < 400
    except requests.RequestException:
        return False


def find_career_page(company_name: str, linkedin_url: str) -> str:
    """
    Find the company's career page URL.

    Strategy:
    1. Get the company website from their LinkedIn page.
    2. Probe common career subdomains (careers.company.com).
    3. Probe common career path suffixes (/careers, /jobs, ...).
    4. Fall back to the base website if nothing more specific is found.
    5. If no website found, attempt a DuckDuckGo HTML search.
    """
    website = get_company_website(linkedin_url)

    if website:
        parsed = urlparse(website)
        base = f"{parsed.scheme}://{parsed.netloc}"
        bare_domain = parsed.netloc.lstrip("www.")

        # Try subdomain variants
        for sub in _CAREER_SUBDOMAINS:
            candidate = f"{parsed.scheme}://{sub}.{bare_domain}"
            if _probe_url(candidate):
                logger.debug(f"Career page via subdomain: {candidate}")
                return candidate

        # Try path variants
        for path in _CAREER_PATHS:
            candidate = base + path
            if _probe_url(candidate):
                logger.debug(f"Career page via path: {candidate}")
                return candidate

        # Fall back to base website
        return base

    # Last resort: if no website found via LinkedIn, nothing more we can do
    logger.debug(f"No website via LinkedIn for '{company_name}', cannot determine career page")
    return ""


# ── Main enrichment pipeline ──────────────────────────────────────────────────

def enrich(gs_config: dict, companies_sheet_name: str) -> None:
    import gspread

    spreadsheet_id = gs_config.get("spreadsheet_id", "")
    jobs_sheet_name = gs_config.get("worksheet", "Jobs")

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

    # Always ensure column D header is present (idempotent)
    comp_ws.update(
        [["Company", "Employee Count", "Career Page", "LinkedIn URL"]],
        "A1:D1",
        value_input_option="USER_ENTERED",
    )

    # ── Task 3: sync new companies from Jobs tab ──────────────────────────────
    # Use Sheets API v4 so we capture the LinkedIn hyperlinks on Company cells
    jobs_company_data = get_jobs_company_linkedin(
        sheets_svc, spreadsheet_id, jobs_sheet_name
    )
    logger.info(f"Jobs tab: {len(jobs_company_data)} unique companies")

    # Read all values in Companies tab (including header)
    all_values = comp_ws.get_all_values()
    existing_names = {
        row[0].strip()
        for row in all_values[1:]
        if row and row[0].strip()
    }

    # Append only companies not already present
    new_companies = sorted(set(jobs_company_data.keys()) - existing_names)
    if new_companies:
        rows_to_add = []
        for name in new_companies:
            linkedin_url = jobs_company_data.get(name, "")
            if linkedin_url:
                # Write as a HYPERLINK formula so enrichment can read the URL
                safe_name = name.replace('"', '""')
                cell_value = f'=HYPERLINK("{linkedin_url}","{safe_name}")'
            else:
                cell_value = name
            rows_to_add.append([cell_value, "", "", ""])
        comp_ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")
        logger.info(f"Added {len(new_companies)} new companies from Jobs tab")
    else:
        logger.info("No new companies to add from Jobs tab")

    # ── Tasks 1 & 2: enrich employee count and career page ────────────────────
    # Re-fetch column A with hyperlink data now that new rows may have been added
    cell_data = get_column_a_data(sheets_svc, spreadsheet_id, companies_sheet_name)
    if not cell_data:
        logger.warning("No data found in Companies tab column A — nothing to enrich")
        return

    # Re-read current B and C values
    all_values = comp_ws.get_all_values()

    enriched_count = 0
    skipped_count = 0

    for row_num, info in sorted(cell_data.items()):
        company_name = info["name"]

        # Fetch current B, C, D values for this row
        row_idx = row_num - 1  # 0-indexed
        if row_idx < len(all_values):
            row_vals = all_values[row_idx]
            current_b = row_vals[1].strip() if len(row_vals) > 1 else ""
            current_c = row_vals[2].strip() if len(row_vals) > 2 else ""
            current_d = row_vals[3].strip() if len(row_vals) > 3 else ""
        else:
            current_b, current_c, current_d = "", "", ""

        # Skip rows already processed: B or C has any value (including "NA"),
        # or D was already tried and found nothing ("NA").
        if current_b or current_c or current_d == "NA":
            skipped_count += 1
            logger.debug(
                f"Row {row_num} '{company_name}' — skipped (already has data: "
                f"B='{current_b}' C='{current_c}' D='{current_d}')"
            )
            continue

        # Resolve LinkedIn URL: column D (skip if "NA") > cell A hyperlink > Jobs tab > slug probe
        linkedin_url = (
            (current_d if current_d and current_d != "NA" else "")
            or info["url"]
            or jobs_company_data.get(company_name, "")
        )

        if not linkedin_url:
            logger.info(f"Row {row_num} '{company_name}' — probing LinkedIn for URL")
            linkedin_url = find_linkedin_url(company_name)

        if not linkedin_url:
            # Write NA so future runs skip this row without re-probing
            _sheets_call(comp_ws.update, [["NA"]], f"D{row_num}", value_input_option="USER_ENTERED")
            _sheets_call(
                comp_ws.format,
                f"D{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
            logger.info(
                f"Row {row_num} '{company_name}' — no LinkedIn URL found, wrote NA to D"
            )
            skipped_count += 1
            continue

        # Write LinkedIn URL to column D if not already there
        if not current_d:
            _sheets_call(
                comp_ws.update,
                [[linkedin_url]],
                f"D{row_num}",
                value_input_option="USER_ENTERED",
            )
            logger.info(f"Row {row_num} '{company_name}' — D written: {linkedin_url}")

        logger.info(f"Row {row_num} | '{company_name}' | {linkedin_url}")

        # Task 1: employee count (LinkedIn only)
        employee_count = scrape_employee_count(linkedin_url)
        logger.info(f"  employee_count: '{employee_count}'")
        time.sleep(_LINKEDIN_DELAY)

        # Task 2: career page
        career_page = find_career_page(company_name, linkedin_url)
        logger.info(f"  career_page: '{career_page}'")
        time.sleep(_LINKEDIN_DELAY)

        # Write B and C — use "NA" when a value could not be found
        out_b = employee_count if employee_count else "NA"
        out_c = career_page if career_page else "NA"
        _sheets_call(
            comp_ws.update,
            [[out_b, out_c]],
            f"B{row_num}:C{row_num}",
            value_input_option="USER_ENTERED",
        )
        # Color cells red where we wrote NA (not found)
        if not employee_count:
            _sheets_call(
                comp_ws.format,
                f"B{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
        if not career_page:
            _sheets_call(
                comp_ws.format,
                f"C{row_num}",
                {"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            )
        enriched_count += 1
        time.sleep(1)  # brief pause between sheet writes

    logger.info(
        f"Done. {enriched_count} rows enriched, {skipped_count} rows skipped (already had data)."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich company data (employee count + career page) in Google Sheets"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config.json (default: config.json)",
    )
    parser.add_argument(
        "--companies-sheet",
        default=None,
        dest="companies_sheet",
        help="Name of the Companies worksheet (overrides config.json google_sheets.companies_worksheet)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)

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
        or gs_config.get("companies_worksheet", "Companies")
    )
    enrich(gs_config, companies_sheet)


if __name__ == "__main__":
    main()
