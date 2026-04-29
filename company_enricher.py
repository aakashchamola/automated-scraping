"""
company_enricher.py — Standalone company enrichment pipeline.

Reads a "Companies" tab in your Google Sheet and:
    - Task 1: fills Employee-Count column (scraped from LinkedIn)
    - Task 2: fills Career-Page column
  - Task 3: syncs unique company names from the Jobs tab into the Companies tab

Rules:
    - Row 1 is a header row; data starts from row 2
    - Column/header mapping is configurable via config.json
    - Only rows where BOTH employee and career columns are empty are processed
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

def get_column_data(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    col_idx: int,
    start_row: int = 2,
) -> dict:
    """
    Returns a dict mapping 1-based row number -> {"value": str, "url": str}
    for every non-empty cell in the selected column (starting at start_row).

    The "url" is the hyperlink attached to the cell (empty string if none).
    Uses the Sheets API v4 includeGridData to access raw hyperlink metadata,
    which works for both formula-based and UI-added hyperlinks.
    """
    col_letter = _col_index_to_letter(col_idx)
    range_notation = f"'{sheet_name}'!{col_letter}{start_row}:{col_letter}"
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
        actual_row = i + start_row
        cells = row.get("values", [])
        if not cells:
            continue
        cell = cells[0]
        value = cell.get("formattedValue", "").strip()
        url = cell.get("hyperlink", "").strip()
        if value:
            data[actual_row] = {"value": value, "url": url}

    return data


def get_jobs_company_linkedin(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    company_header: str = "Company",
) -> dict:
    """
    Read the configured company column from the source tab using the Sheets API v4,
    returning a dict mapping company_name -> linkedin_url ("" when no hyperlink).
    Only the first occurrence of each company name is kept.
    """
    # Find which column index holds the configured company header
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
        col_idx = header_row.index(company_header)
    except ValueError:
        logger.warning(
            f"No '{company_header}' column found in '{sheet_name}' header"
        )
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


def get_source_sheet_career_pages(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    company_header: str = "Company",
    career_page_header: str = "Career-Page",
) -> dict:
    """
    Read company name -> career page URL mapping from the source sheet.
    Uses Sheets API v4 to capture cell values.
    Only the first occurrence of each company name is kept.
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        header_resp = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!1:1")
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Could not read {sheet_name} header: {exc}")
        return {}

    header_row = header_resp.get("values", [[]])
    header_row = header_row[0] if header_row else []

    try:
        company_col_idx = header_row.index(company_header)
    except ValueError:
        logger.warning(f"No '{company_header}' column in '{sheet_name}'")
        return {}

    try:
        career_col_idx = header_row.index(career_page_header)
    except ValueError:
        logger.debug(f"No '{career_page_header}' column in '{sheet_name}'")
        return {}

    company_col_letter = _col_index_to_letter(company_col_idx)
    career_col_letter = _col_index_to_letter(career_col_idx)
    range_notation = f"'{sheet_name}'!{company_col_letter}2:{company_col_letter},{career_col_letter}2:{career_col_letter}"

    try:
        result = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_notation)
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Sheets API call failed reading career pages: {exc}")
        return {}

    rows = result.get("values", [])
    data = {}

    for row in rows:
        if len(row) < 2:
            continue
        company_name = (row[0] or "").strip()
        career_page = (row[1] or "").strip()
        if company_name and company_name not in data and career_page:
            data[company_name] = career_page

    return data


def _header_index_map(header_row: list) -> dict:
    """Case-insensitive header -> index mapping for row 1 values."""
    mapping = {}
    for idx, raw in enumerate(header_row):
        key = (raw or "").strip().lower()
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def _normalize_header_key(value: str) -> str:
    """Normalize header labels for tolerant matching (e.g., LinkedIn URL variants)."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _header_aliases(field: str, desired_header: str) -> set:
    """Known synonyms to avoid creating duplicate semantic columns."""
    desired = (desired_header or "").strip()
    aliases = {desired}
    if field == "company":
        aliases.update({"Company", "Companies"})
    elif field == "employee_count":
        aliases.update({"Employee Count", "Employee-Count"})
    elif field == "career_page":
        aliases.update({"Career Page", "Career-Page"})
    elif field == "linkedin_url":
        aliases.update({"LinkedIn URL", "Linkedin-Url", "LinkedIn-Url", "Linkedin URL"})
    return {a for a in aliases if a}


def _required_company_columns(gs_config: dict) -> dict:
    """
    Configurable Companies-sheet headers.
    Defaults preserve current behavior but allow custom headers like:
      Company: "Companies"
      LinkedIn URL: "Linkedin-Url"
    """
    cfg = gs_config.get("companies_columns", {}) if isinstance(gs_config, dict) else {}
    return {
        "company": cfg.get("company", "Company"),
        "employee_count": cfg.get("employee_count", "Employee Count"),
        "career_page": cfg.get("career_page", "Career Page"),
        "linkedin_url": cfg.get("linkedin_url", "LinkedIn URL"),
    }


def _enrichment_controls(gs_config: dict) -> dict:
    """
    Controls for retry behavior during enrichment.

    retry_na_fields:
      - False (default): treat NA as already processed (legacy behavior)
      - True: treat NA as pending and try again

    retry_invalid_career_values:
      - list of exact values to treat as invalid/pending for career page
      - default includes '://'
    """
    cfg = gs_config.get("enrichment_controls", {}) if isinstance(gs_config, dict) else {}
    raw_invalid = cfg.get("retry_invalid_career_values", ["://"])
    if not isinstance(raw_invalid, list):
        raw_invalid = ["://"]
    invalid_values = {
        str(v).strip().lower() for v in raw_invalid if str(v).strip()
    }
    return {
        "retry_na_fields": bool(cfg.get("retry_na_fields", False)),
        "retry_invalid_career_values": invalid_values,
    }


def _is_na(value: str) -> bool:
    return (value or "").strip().upper() == "NA"


def _is_invalid_career_value(value: str, invalid_values: set) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    if v in invalid_values:
        return True
    # Guard against malformed values like just scheme fragments.
    if v == "://":
        return True
    return False


def _normalize_employee_count(value: str) -> str:
    """Return numeric-only employee count as digits, or empty when unavailable."""
    raw = (value or "").strip()
    if not raw:
        return ""
    nums = re.findall(r"\d[\d,]*", raw)
    if not nums:
        return ""
    return nums[0].replace(",", "")


def _is_numeric_employee_count(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    # Accept both plain digits and comma-formatted counts (e.g., 79222 / 79,222).
    return bool(re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)", v))


def _ensure_required_headers(comp_ws, required_headers: dict) -> dict:
    """
    Ensure required header names exist in row 1, creating missing headers at row end.
    Returns a mapping of logical field -> column index.
    """
    header_values = comp_ws.row_values(1)
    index_by_header = _header_index_map(header_values)
    normalized_header_index = {}
    for idx, raw in enumerate(header_values):
        nkey = _normalize_header_key(raw)
        if nkey and nkey not in normalized_header_index:
            normalized_header_index[nkey] = idx

    field_indexes = {}
    changed = False

    for field, header in required_headers.items():
        idx = index_by_header.get(header.strip().lower())
        if idx is None:
            for alias in _header_aliases(field, header):
                idx = normalized_header_index.get(_normalize_header_key(alias))
                if idx is not None:
                    logger.info(
                        f"Using existing header '{header_values[idx]}' for '{field}'"
                    )
                    break
        if idx is None:
            idx = len(header_values)
            header_values.append(header)
            index_by_header[header.strip().lower()] = idx
            normalized_header_index[_normalize_header_key(header)] = idx
            changed = True
            logger.info(
                f"Added missing header '{header}' at column {_col_index_to_letter(idx)}"
            )
        field_indexes[field] = idx

    if changed:
        end_col = _col_index_to_letter(len(header_values) - 1)
        _sheets_call(
            comp_ws.update,
            [header_values],
            f"A1:{end_col}1",
            value_input_option="USER_ENTERED",
        )

    return field_indexes


# ── LinkedIn scraping ─────────────────────────────────────────────────────────

def scrape_employee_count(linkedin_url: str) -> str:
    """
    Scrape the employee count from a LinkedIn company /about page.
    Returns a string like "1,001-5,000 employees" or "" if not found.
    LinkedIn aggressively rate-limits; gracefully return "" on failure.
    """
    if not linkedin_url or "linkedin.com" not in linkedin_url:
        return ""

    def _canonical_company_base(url: str) -> str:
        m = re.search(r"(https?://(?:www\.)?linkedin\.com/company/[^/?#]+)", url or "")
        if not m:
            return ""
        return m.group(1).rstrip("/") + "/"

    initial_base = _canonical_company_base(linkedin_url) or (linkedin_url.rstrip("/") + "/")
    queue = [initial_base + "people/", initial_base]
    seen = set()

    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        try:
            resp = requests.get(
                url, headers=_BROWSER_HEADERS, timeout=12, allow_redirects=True
            )
        except requests.RequestException as exc:
            logger.warning(f"LinkedIn request failed for {url}: {exc}")
            continue

        # If LinkedIn canonicalized company slug, enqueue canonical /people/ + base.
        redirected_base = _canonical_company_base(resp.url)
        if redirected_base and redirected_base != initial_base:
            for candidate in (redirected_base + "people/", redirected_base):
                if candidate not in seen and candidate not in queue:
                    queue.append(candidate)
            logger.debug(
                f"LinkedIn redirected company URL; queued canonical fallback: {redirected_base}"
            )

        if resp.status_code == 999:
            logger.warning(f"LinkedIn rate-limited (999) for {url}")
            continue
        if resp.status_code != 200:
            logger.warning(f"LinkedIn returned HTTP {resp.status_code} for {url}")
            continue
        # If redirected to login, skip this variant and try fallback
        if "login" in resp.url or "authwall" in resp.url or "/company/" not in resp.url:
            logger.warning(f"LinkedIn redirected to login/authwall for {url}")
            continue

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
                value = match.group(1).strip()
                # People page patterns usually return a number; normalize label.
                if value.replace(",", "").isdigit():
                    return f"{value} employees"
                return value

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
    source_sheet_name = gs_config.get(
        "company_source_worksheet", gs_config.get("worksheet", "Jobs")
    )
    source_company_header = gs_config.get("company_source_header", "Company")

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
    required_headers = _required_company_columns(gs_config)
    col_idx = _ensure_required_headers(comp_ws, required_headers)
    controls = _enrichment_controls(gs_config)
    retry_na_fields = controls["retry_na_fields"]
    retry_invalid_career_values = controls["retry_invalid_career_values"]

    # ── Task 3: sync new companies from Jobs tab ──────────────────────────────
    # Use Sheets API v4 so we capture the LinkedIn hyperlinks on Company cells
    jobs_company_data = get_jobs_company_linkedin(
        sheets_svc,
        spreadsheet_id,
        source_sheet_name,
        company_header=source_company_header,
    )
    logger.info(
        f"Source tab '{source_sheet_name}' ({source_company_header}): "
        f"{len(jobs_company_data)} unique companies"
    )

    # Read career pages from source sheet as fallback
    source_career_data = get_source_sheet_career_pages(
        sheets_svc,
        spreadsheet_id,
        source_sheet_name,
        company_header=source_company_header,
        career_page_header="Career-Page",
    )
    logger.info(
        f"Source tab career pages available for {len(source_career_data)} companies"
    )

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
        logger.info(
            f"No new companies to add from source tab '{source_sheet_name}'"
        )

    # ── Tasks 1 & 2: enrich employee count and career page ────────────────────
    # Re-fetch company column values with hyperlink metadata after appends.
    cell_data = get_column_data(
        sheets_svc,
        spreadsheet_id,
        companies_sheet_name,
        company_i,
        start_row=2,
    )
    if not cell_data:
        logger.warning("No data found in Companies tab company column — nothing to enrich")
        return

    # Re-read current B and C values
    all_values = comp_ws.get_all_values()

    enriched_count = 0
    skipped_count = 0

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

        employee_done = (
            bool(current_b)
            and _is_numeric_employee_count(current_b)
            and not (retry_na_fields and _is_na(current_b))
        )
        career_done = (
            bool(current_c)
            and not (retry_na_fields and _is_na(current_c))
            and not _is_invalid_career_value(current_c, retry_invalid_career_values)
        )
        linkedin_locked = _is_na(current_d) and not retry_na_fields

        need_employee = not employee_done
        need_career = not career_done

        # Skip only if both target fields are already satisfied, or if LinkedIn
        # was previously marked NA and NA-retry mode is disabled.
        if (not need_employee and not need_career) or linkedin_locked:
            skipped_count += 1
            logger.debug(
                f"Row {row_num} '{company_name}' — skipped (already has data: "
                f"B='{current_b}' C='{current_c}' D='{current_d}')"
            )
            continue

        # Resolve LinkedIn URL: configured LinkedIn column (skip if "NA")
        # > company-cell hyperlink > Jobs tab > slug probe
        linkedin_url = (
            (current_d if current_d and not _is_na(current_d) else "")
            or info["url"]
            or jobs_company_data.get(company_name, "")
        )

        if not linkedin_url:
            logger.info(f"Row {row_num} '{company_name}' — probing LinkedIn for URL")
            linkedin_url = find_linkedin_url(company_name)

        if not linkedin_url:
            # Write NA so future runs skip this row without re-probing
            linkedin_col = _col_index_to_letter(linkedin_i)
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
        if not current_d or _is_na(current_d):
            linkedin_col = _col_index_to_letter(linkedin_i)
            _sheets_call(
                comp_ws.update,
                [[linkedin_url]],
                f"{linkedin_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
            logger.info(f"Row {row_num} '{company_name}' — LinkedIn URL written: {linkedin_url}")

        logger.info(f"Row {row_num} | '{company_name}' | {linkedin_url}")

        # Task 1: employee count (LinkedIn only)
        employee_count = ""
        if need_employee:
            employee_raw = scrape_employee_count(linkedin_url)
            employee_count = _normalize_employee_count(employee_raw)
            logger.info(
                f"  employee_count_raw: '{employee_raw}' | employee_count: '{employee_count}'"
            )
            time.sleep(_LINKEDIN_DELAY)

        # Task 2: career page
        career_page = ""
        if need_career:
            career_page = find_career_page(company_name, linkedin_url)
            # Fallback to source sheet career page if not found via LinkedIn enrichment
            if not career_page:
                career_page = source_career_data.get(company_name, "")
                if career_page:
                    logger.debug(f"  career_page (from source): {career_page}")
            logger.info(f"  career_page: '{career_page}'")
            time.sleep(_LINKEDIN_DELAY)

        # Write only pending fields — use "NA" when a value could not be found
        b_col = _col_index_to_letter(employee_i)
        c_col = _col_index_to_letter(career_i)
        if need_employee:
            out_b = employee_count if employee_count else "NA"
            _sheets_call(
                comp_ws.update,
                [[out_b]],
                f"{b_col}{row_num}",
                value_input_option="USER_ENTERED",
            )
        if need_career:
            out_c = career_page if career_page else "NA"
            _sheets_call(
                comp_ws.update,
                [[out_c]],
                f"{c_col}{row_num}",
                value_input_option="USER_ENTERED",
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
