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
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime

import requests

import projects_registry
from config_loader import load_config
import remote_store
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
    sheet_store,
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
    pending_status = {}   # row_num -> status, written as one column at the end

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
            # Queued, not written. One update_cell per row costs one API write
            # per row, and Sheets allows sixty a minute — so a few hundred rows
            # hit a 429, every further write failed, and the run still reported
            # success over a half-filled column. The whole column goes out in
            # one batched call below instead.
            pending_status[row_num] = status
            updated += 1
            logger.info(f"Row {row_num}: '{status}'  ({url[:70]})")

        # Always queue a row color — regardless of whether the status changed
        row_colors.append((row_num, _status_color(status)))

        if checked % delay_every == 0:
            logger.info(f"...validated {checked} jobs")
            time.sleep(delay_sec)

        if limit and checked >= limit:
            logger.info(f"Reached --limit {limit}; stopping")
            break

    # One batched write for the whole status column. Rows that were skipped or
    # unchanged keep what they already had, so the existing value is written
    # back rather than blanked.
    write_failed = None
    if pending_status:
        highest = max(pending_status)
        column = []
        for row_num in range(2, highest + 1):
            if row_num in pending_status:
                column.append([pending_status[row_num]])
            else:
                source = rows[row_num - 1]
                column.append([source[status_idx] if status_idx < len(source) else ""])
        try:
            sheet_store.write_column_values(
                status_col_pos, column, worksheet, start_row=2)
        except Exception as exc:
            # Never let a failed write pass for a completed validation.
            write_failed = exc
            logger.error(f"Failed to write the status column: {exc}")

    # Batch-apply all row background colors in one API call
    if row_colors:
        logger.info(f"Applying row colors to {len(row_colors)} rows…")
        try:
            sheet_store.batch_format_rows(row_colors, num_cols=num_cols, worksheet_name=worksheet)
        except Exception as exc:
            logger.warning(f"Batch row formatting failed: {exc}")

    if write_failed is not None:
        # Loudly, and as a failure. The previous version logged one error per
        # row and returned a summary that read like a clean run, so a
        # half-validated sheet looked finished.
        raise RuntimeError(
            f"validated {checked} jobs but could not write the '{status_column}' "
            f"column, so the sheet is unchanged: {write_failed}")

    logger.info(
        f"Validation done. checked={checked} updated={updated} | "
        f"active={summary[STATUS_ACTIVE]} removed={summary[STATUS_REMOVED]} "
        f"expired={summary[STATUS_EXPIRED]} unknown={summary[STATUS_UNKNOWN]}"
    )
    return summary


def remove_rows_by_status(
    sheet_store, worksheet: str, status_column: str, statuses: list,
) -> int:
    """Delete rows whose status is in *statuses*. Returns how many went.

    Every deleted row is written to a CSV under logs/ first. A status column is
    a judgement made by an HTTP probe — a site that 403s a datacenter IP looks
    identical to a closed posting — so the rows have to be recoverable.

    Rows are deleted bottom-up: deleting row 5 renumbers everything below it,
    so working downwards would delete the wrong rows after the first one.
    """
    wanted = {s.strip().lower() for s in statuses if s and s.strip()}
    if not wanted:
        logger.info("No statuses selected for removal; nothing to do")
        return 0

    rows = sheet_store.load_all_rows(worksheet)
    if len(rows) < 2:
        return 0
    header = rows[0]
    if status_column not in header:
        logger.warning(f"Status column '{status_column}' missing; nothing removed")
        return 0
    status_idx = header.index(status_column)

    doomed = [
        (num, row) for num, row in enumerate(rows[1:], start=2)
        if status_idx < len(row) and row[status_idx].strip().lower() in wanted
    ]
    if not doomed:
        logger.info(f"No rows matched {sorted(wanted)}; nothing removed")
        return 0

    os.makedirs("logs", exist_ok=True)
    backup = os.path.join(
        "logs", f"removed_rows_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(backup, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["_sheet_row"] + header)
        for num, row in doomed:
            writer.writerow([num] + row)
    logger.info(f"Backed up {len(doomed)} rows to {backup} before deleting")

    # Via the store, not a gspread worksheet: the same call then works whether
    # this machine has the service-account key or only a project password.
    deleted = sheet_store.delete_rows(worksheet, [num for num, _ in doomed])
    logger.info(f"Removed {deleted} row(s) with status in {sorted(wanted)}")
    return deleted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate job postings and write status into Google Sheets"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--worksheet",
        default=None,
        help="Jobs worksheet to validate (default: google_sheets.jobs_worksheet from config)",
    )
    parser.add_argument("--url-column", default="Job Link")
    parser.add_argument("--status-column", default="Job Status")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Validate at most N jobs (0 = all). Useful for quick test runs.",
    )
    parser.add_argument(
        "--no-remove", action="store_true", dest="no_remove",
        help="Validate only; never delete rows even if remove_rows is on.",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config, name="validate")
    gs_config = config.get("google_sheets", {})

    if not gs_config.get("enabled"):
        logger.error("Google Sheets is not enabled in config.yaml")
        sys.exit(1)

    worksheet = args.worksheet or gs_config.get("jobs_worksheet", "Jobs")
    validation_cfg = config.get("job_validation", {})
    re_validate = validation_cfg.get("re_validate", True)
    store = remote_store.store_for(config)
    validate_jobs(
        store,
        worksheet=worksheet,
        job_url_column=args.url_column,
        status_column=args.status_column,
        limit=args.limit,
        re_validate=re_validate,
    )

    # Optional second pass: delete the rows the statuses just condemned. Off by
    # default, and it runs only after validation, so what gets deleted is
    # always judged on statuses written moments earlier rather than stale ones.
    remove_rows = validation_cfg.get("remove_rows", False)
    if args.no_remove:
        remove_rows = False
    if remove_rows:
        statuses = validation_cfg.get("remove_statuses") or []
        logger.info(f"Row removal is ON for statuses: {statuses or '(none selected)'}")
        remove_rows_by_status(store, worksheet, args.status_column, statuses)
    else:
        logger.info("Row removal is off (job_validation.remove_rows)")
