"""
job_validator.py — Job validation service.

Probes each job URL in the Jobs sheet and writes a status into a status column
(auto-created if missing):

    Active   — URL responds 2xx
    Removed  — URL responds 404 / 410 (posting gone)
    Expired  — URL responds other 4xx (e.g. requires login / closed)
    Unknown  — network error or 5xx (transient; left for next run)

Usage:
    python job_validator.py --config config.json
    python job_validator.py --config config.json --worksheet Jobs_Test
"""

import argparse
import logging
import re
import sys
import time

import requests

from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

STATUS_ACTIVE = "Active"
STATUS_EXPIRED = "Expired"
STATUS_REMOVED = "Removed"
STATUS_UNKNOWN = "Unknown"

# Row background colors (normalized RGB 0-1)
_COLOR_YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.6}   # Unknown
_COLOR_RED    = {"red": 1.0, "green": 0.6, "blue": 0.6}   # Expired / Removed


def _status_color(status: str):
    """Return the row bg_color for a status, or None for Active (leave white)."""
    if status == STATUS_UNKNOWN:
        return _COLOR_YELLOW
    if status in (STATUS_EXPIRED, STATUS_REMOVED):
        return _COLOR_RED
    return None


def _status_for_code(code: int) -> str:
    if 200 <= code < 400:
        return STATUS_ACTIVE
    if code in (404, 410):
        return STATUS_REMOVED
    if 400 <= code < 500:
        return STATUS_EXPIRED
    return STATUS_UNKNOWN


# LinkedIn job id appears as the trailing number of a /jobs/view/ slug or as
# a currentJobId query param.
_LINKEDIN_JOB_ID_RE = re.compile(r"jobs/view/(?:[^/?#]*-)?(\d{6,})|currentJobId=(\d{6,})")

# LinkedIn serves a 200 page for CLOSED jobs with one of these banners; the
# public /jobs/view/ URL renders this via JS (invisible to scraping), so we
# read it from the guest jobPosting endpoint instead.
_LINKEDIN_EXPIRED_MARKERS = (
    "no longer accepting",
    "no longer available",
    "this job is no longer",
    "not accepting applications",
)


def _linkedin_job_id(url: str) -> str:
    """Extract the numeric LinkedIn job id from a job URL, or ''."""
    m = _LINKEDIN_JOB_ID_RE.search(url or "")
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


def _check_linkedin(url: str, timeout: int):
    """Status for a LinkedIn job link, or None if no job id is present.

    Uses the guest jobPosting endpoint:
      - 404            -> Removed (posting gone)
      - 200 + banner   -> Expired ("no longer accepting applications")
      - 200 otherwise  -> Active
      - other/error    -> Unknown
    """
    jid = _linkedin_job_id(url)
    if not jid:
        return None
    api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
    try:
        resp = requests.get(
            api, headers=_BROWSER_HEADERS, timeout=timeout, allow_redirects=True
        )
    except requests.RequestException as exc:
        logger.debug(f"LinkedIn guest check failed for {url}: {exc}")
        return STATUS_UNKNOWN
    if resp.status_code == 404:
        return STATUS_REMOVED
    if resp.status_code != 200:
        return STATUS_UNKNOWN
    text = resp.text.lower()
    if any(marker in text for marker in _LINKEDIN_EXPIRED_MARKERS):
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def check_job_url(url: str, timeout: int = 10) -> str:
    """Return Active / Removed / Expired / Unknown for a job URL.

    LinkedIn job links are checked via the guest jobPosting endpoint (the only
    reliable way to see a closed-job banner). Other URLs use HEAD, falling back
    to GET when the server blocks HEAD (403/405).
    """
    if not url or not url.startswith("http"):
        return STATUS_UNKNOWN

    if "linkedin.com" in url:
        li_status = _check_linkedin(url, timeout)
        if li_status is not None:
            return li_status

    try:
        resp = requests.head(
            url, headers=_BROWSER_HEADERS, timeout=timeout, allow_redirects=True
        )
        if resp.status_code in (403, 405):
            resp = requests.get(
                url, headers=_BROWSER_HEADERS, timeout=timeout, allow_redirects=True
            )
    except requests.RequestException as exc:
        logger.debug(f"check failed for {url}: {exc}")
        return STATUS_UNKNOWN

    return _status_for_code(resp.status_code)


def validate_jobs(
    sheet_store: GoogleSheetsStore,
    worksheet: str,
    job_url_column: str = "Job Link",
    status_column: str = "Job Status",
    delay_every: int = 10,
    delay_sec: float = 2.0,
    limit: int = 0,
    re_validate: bool = True,
) -> dict:
    """Validate jobs in ``worksheet``; write statuses into ``status_column``.

    re_validate=False  — skip rows that already have a status (read from
                         config ``job_validation.re_validate``). Coloring is
                         still applied to every row regardless of this flag.
    limit > 0          — stop after that many jobs checked (quick test runs).
    Returns a summary dict of counts.
    """
    logger.info(
        f"Validating | worksheet='{worksheet}' url_col='{job_url_column}' "
        f"status_col='{status_column}' re_validate={re_validate}"
    )

    rows = sheet_store.load_all_rows(worksheet)
    if not rows:
        logger.warning(f"No data in '{worksheet}'")
        return {}

    header = rows[0]
    if job_url_column not in header:
        logger.error(f"URL column '{job_url_column}' not in header: {header}")
        return {}

    url_idx = header.index(job_url_column)
    # Auto-create the status column if missing; returns 1-indexed position.
    status_col_pos = sheet_store.ensure_column(status_column, worksheet)
    status_idx = status_col_pos - 1
    num_cols = len(header)

    summary = {STATUS_ACTIVE: 0, STATUS_REMOVED: 0, STATUS_EXPIRED: 0, STATUS_UNKNOWN: 0}
    checked = 0
    updated = 0
    row_colors = []   # (row_num, bg_color or None) — collected for one batch call

    for row_num, row in enumerate(rows[1:], start=2):
        url = row[url_idx].strip() if url_idx < len(row) else ""
        if not url:
            continue

        existing = row[status_idx].strip() if status_idx < len(row) else ""

        # Skip re-checking when re_validate is off and row is already done.
        # Still record the existing color so the row gets painted correctly.
        if not re_validate and existing:
            row_colors.append((row_num, _status_color(existing)))
            continue

        status = check_job_url(url)
        summary[status] = summary.get(status, 0) + 1
        checked += 1

        if existing != status:
            try:
                sheet_store.update_cell(row_num, status_col_pos, status, worksheet)
                updated += 1
                logger.info(f"Row {row_num}: '{status}'  ({url[:70]})")
            except Exception as exc:
                logger.error(f"Failed to update row {row_num}: {exc}")

        # Always queue a row color — regardless of whether the status changed
        row_colors.append((row_num, _status_color(status)))

        if checked % delay_every == 0:
            logger.info(f"...validated {checked} jobs")
            time.sleep(delay_sec)

        if limit and checked >= limit:
            logger.info(f"Reached --limit {limit}; stopping")
            break

    # Batch-apply all row background colors in one API call
    if row_colors:
        logger.info(f"Applying row colors to {len(row_colors)} rows…")
        try:
            sheet_store.batch_format_rows(row_colors, num_cols=num_cols, worksheet_name=worksheet)
        except Exception as exc:
            logger.warning(f"Batch row formatting failed: {exc}")

    logger.info(
        f"Validation done. checked={checked} updated={updated} | "
        f"active={summary[STATUS_ACTIVE]} removed={summary[STATUS_REMOVED]} "
        f"expired={summary[STATUS_EXPIRED]} unknown={summary[STATUS_UNKNOWN]}"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate job postings and write status into Google Sheets"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--worksheet",
        default=None,
        help="Worksheet to validate (default: google_sheets.worksheet from config)",
    )
    parser.add_argument("--url-column", default="Job Link")
    parser.add_argument("--status-column", default="Job Status")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Validate at most N jobs (0 = all). Useful for quick test runs.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    setup_logging_from_config(config, name="validate")
    gs_config = config.get("google_sheets", {})

    if not gs_config.get("enabled"):
        logger.error("Google Sheets is not enabled in config.json")
        sys.exit(1)

    worksheet = args.worksheet or gs_config.get("jobs_worksheet", "Jobs")
    validation_cfg = config.get("job_validation", {})
    re_validate = validation_cfg.get("re_validate", True)
    store = GoogleSheetsStore(gs_config)
    validate_jobs(
        store,
        worksheet=worksheet,
        job_url_column=args.url_column,
        status_column=args.status_column,
        limit=args.limit,
        re_validate=re_validate,
    )
