"""cleanup_validation.py — Safely clear stale rows from a target Companies sheet.

Behavior:
- Reads company names from Jobs and Company tabs.
- Builds an allowed set using unique normalized names from both tabs.
- Optionally syncs missing allowed company names into target tab.
- In target tab (e.g. CompaniesTest), clears rows whose company name is not in allowed set.
- Resets cleared row background to white.
- Does NOT modify Jobs or Company tabs.

Extra output:
- Extracts LinkedIn company/school profile URLs from Jobs job-link column and writes
  a report CSV for future enrichment improvements.

Usage:
    python cleanup_validation.py
    python cleanup_validation.py --config cleanup_validation_config.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import projects_registry
from google_sheets_store import sheets_call
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:company|school)/[^/?#]+",
    re.IGNORECASE,
)


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _col_index_to_letter(idx: int) -> str:
    result = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _header_index_map(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        key = (raw or "").strip().lower()
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def _extract_linkedin_company_url(raw_url: str) -> str:
    """Extract canonical LinkedIn company/school URL from any job-link style URL."""
    if not raw_url:
        return ""

    candidates = [raw_url]

    # Unwrap query params that contain nested/encoded URLs.
    try:
        parsed = urlparse(raw_url)
        for values in parse_qs(parsed.query).values():
            for value in values:
                if value and "http" in value:
                    candidates.append(value)
                    candidates.append(unquote(value))
                    candidates.append(unquote(unquote(value)))
    except Exception:
        pass

    for item in candidates:
        if not item:
            continue
        text = unquote(unquote(item))
        match = _LINKEDIN_COMPANY_RE.search(text)
        if not match:
            continue
        found = match.group(0).rstrip("/") + "/"
        found = re.sub(r"^https?://([a-z]{2,3}\.)?www\.linkedin\.com", "https://www.linkedin.com", found, flags=re.IGNORECASE)
        return found

    return ""


def _load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.error("Config not found: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error("Config JSON invalid: %s", exc)
        sys.exit(1)


def _build_credentials(gs_cfg: dict[str, Any]):
    from google.oauth2.service_account import Credentials

    creds_file = gs_cfg.get("credentials_file", "")
    if not creds_file:
        raise RuntimeError("Missing google_sheets.credentials_file in cleanup config.")
    return Credentials.from_service_account_file(creds_file, scopes=_SCOPES)


def _gspread_client(creds):
    import gspread

    return gspread.authorize(creds)


def _sheets_call(fn, *args, **kwargs):
    """Retry wrapper for gspread calls — see google_sheets_store.sheets_call.

    Kept as a thin alias because this module calls it in several places. The
    implementation is shared so the two copies that used to live here and in
    company_enricher.py cannot drift apart again.
    """
    return sheets_call(fn, *args, **kwargs)


def _get_worksheet(spreadsheet, name: str):
    import gspread

    try:
        ws = spreadsheet.worksheet(name)
        logger.info("Found worksheet '%s'", name)
        return ws
    except gspread.WorksheetNotFound as exc:
        raise RuntimeError(f"Worksheet '{name}' was not found.") from exc


def _get_column_values_by_header(ws, header_name: str) -> tuple[list[str], int, list[list[str]]]:
    all_values = _sheets_call(ws.get_all_values)
    if not all_values:
        return [], -1, []

    header = all_values[0]
    idx_map = _header_index_map(header)
    idx = idx_map.get(header_name.strip().lower())
    if idx is None:
        return [], -1, all_values

    values: list[str] = []
    for row in all_values[1:]:
        values.append(row[idx].strip() if len(row) > idx else "")
    return values, idx, all_values


def _write_backup_csv(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = ["row_num", "company", "reason", "row_values"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_linkedin_report(path: str, mapping: dict[str, set[str]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["company", "linkedin_url"])
        for company in sorted(mapping):
            for url in sorted(mapping[company]):
                writer.writerow([company, url])


def _format_output_path(path_template: str, now: str) -> str:
    return (path_template or "").format(now=now)


def run_cleanup(config: dict[str, Any]) -> None:
    import remote_store
    if remote_store.is_configured():
        # First, before any config is validated: what this machine can do does
        # not depend on how the file is filled in. Portable in principle — the
        # Web App can read a tab and delete rows — but every read below is
        # written around a live gspread worksheet, so it is a port rather than
        # a switch. Said plainly instead of failing on a missing file: this one
        # names its key in its OWN config, so the path in that error was one
        # the reader had no way to place.
        raise remote_store.NeedsGoogleKey(
            "cleanup-rows still needs the Google key. Run it on the machine "
            "that holds the service-account file.")

    cleanup_cfg = config.get("cleanup_validation", {})
    if not isinstance(cleanup_cfg, dict):
        raise RuntimeError("cleanup_validation section must be an object.")

    gs_cfg = cleanup_cfg.get("google_sheets", {})
    sheets_cfg = cleanup_cfg.get("sheets", {})
    safety_cfg = cleanup_cfg.get("safety", {})
    behavior_cfg = cleanup_cfg.get("behavior", {})

    spreadsheet_id = gs_cfg.get("spreadsheet_id", "")
    if not spreadsheet_id:
        raise RuntimeError("Missing cleanup_validation.google_sheets.spreadsheet_id")

    jobs_sheet_name = sheets_cfg.get("jobs", {}).get("name", "Jobs")
    jobs_company_header = sheets_cfg.get("jobs", {}).get("company_header", "Company")
    jobs_link_header = sheets_cfg.get("jobs", {}).get("job_link_header", "Job Link")

    source_sheet_name = sheets_cfg.get("source", {}).get("name", "Company")
    source_company_header = sheets_cfg.get("source", {}).get("company_header", "Company")

    target_sheet_name = sheets_cfg.get("target", {}).get("name", "CompaniesTest")
    target_company_header = sheets_cfg.get("target", {}).get("company_header", "Company")

    dry_run = bool(safety_cfg.get("dry_run", True))
    sync_missing_to_target = bool(behavior_cfg.get("sync_missing_to_target", True))

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_csv = _format_output_path(
        safety_cfg.get(
            "backup_csv",
            os.path.join("logs", "cleanup_rows_backup_{now}.csv"),
        ),
        now,
    )
    linkedin_report_csv = _format_output_path(
        safety_cfg.get(
            "linkedin_report_csv",
            os.path.join("logs", "jobs_linkedin_company_candidates_{now}.csv"),
        ),
        now,
    )

    creds = _build_credentials(gs_cfg)
    gc = _gspread_client(creds)

    spreadsheet = _sheets_call(gc.open_by_key, spreadsheet_id)
    jobs_ws = _get_worksheet(spreadsheet, jobs_sheet_name)
    source_ws = _get_worksheet(spreadsheet, source_sheet_name)
    target_ws = _get_worksheet(spreadsheet, target_sheet_name)

    jobs_companies, _, jobs_all = _get_column_values_by_header(jobs_ws, jobs_company_header)
    jobs_links, jobs_link_idx, _ = _get_column_values_by_header(jobs_ws, jobs_link_header)
    source_companies, _, _ = _get_column_values_by_header(source_ws, source_company_header)

    jobs_set = {_normalize_name(v) for v in jobs_companies if v.strip()}
    source_set = {_normalize_name(v) for v in source_companies if v.strip()}

    # Keep first-seen display values so synced rows preserve human-friendly names.
    allowed_display: dict[str, str] = {}
    for name in jobs_companies:
        stripped = (name or "").strip()
        if not stripped:
            continue
        norm = _normalize_name(stripped)
        if norm and norm not in allowed_display:
            allowed_display[norm] = stripped
    for name in source_companies:
        stripped = (name or "").strip()
        if not stripped:
            continue
        norm = _normalize_name(stripped)
        if norm and norm not in allowed_display:
            allowed_display[norm] = stripped

    allowed = set(allowed_display.keys())
    if not allowed:
        # Every target row is stale when nothing is allowed, so this would
        # clear the whole sheet. It happens for a boring reason — a renamed or
        # missing company header makes _read_column return nothing — and the
        # run would look like a successful cleanup.
        raise SystemExit(
            "refusing to run: the allowed-company list is empty, which would "
            "clear every row in the target sheet. Check that the Jobs and "
            "Company tabs still have their company-name headers.")

    logger.info("Jobs unique companies: %s", len(jobs_set))
    logger.info("Source unique companies: %s", len(source_set))
    logger.info("Allowed unique companies (union): %s", len(allowed))

    # Build LinkedIn company URL candidates from jobs links for future enrichment use.
    linkedin_by_company: dict[str, set[str]] = defaultdict(set)
    if jobs_link_idx >= 0:
        for row_idx, company in enumerate(jobs_companies, start=2):
            if not company.strip():
                continue
            link = jobs_links[row_idx - 2] if row_idx - 2 < len(jobs_links) else ""
            linkedin_url = _extract_linkedin_company_url(link)
            if linkedin_url:
                linkedin_by_company[company.strip()].add(linkedin_url)
        _write_linkedin_report(linkedin_report_csv, linkedin_by_company)
        logger.info(
            "LinkedIn candidates from Jobs links: %s companies | report=%s",
            len(linkedin_by_company),
            linkedin_report_csv,
        )
    else:
        logger.warning(
            "Jobs link header '%s' not found in sheet '%s'; skipped link extraction report.",
            jobs_link_header,
            jobs_sheet_name,
        )

    target_company_values, target_company_idx, target_all = _get_column_values_by_header(
        target_ws,
        target_company_header,
    )
    if target_company_idx < 0:
        raise RuntimeError(
            f"Target company header '{target_company_header}' not found in '{target_sheet_name}'."
        )

    target_width = max((len(row) for row in target_all), default=1)
    target_end_col = _col_index_to_letter(max(0, target_width - 1))

    # Optional sync: add company names present in Jobs/Company union but missing in target.
    target_norm_set = {_normalize_name(v) for v in target_company_values if (v or "").strip()}
    missing_norms = sorted(norm for norm in allowed if norm not in target_norm_set)
    logger.info("Allowed companies missing in target: %s", len(missing_norms))

    if sync_missing_to_target and missing_norms:
        if dry_run:
            logger.info("Dry-run enabled. Target sync preview only; no rows appended.")
        else:
            rows_to_add: list[list[str]] = []
            for norm in missing_norms:
                company_name = allowed_display.get(norm, norm)
                row = [""] * target_width
                row[target_company_idx] = company_name
                rows_to_add.append(row)
            _sheets_call(target_ws.append_rows, rows_to_add, value_input_option="USER_ENTERED")
            logger.info(
                "Synced %s missing companies into '%s'.",
                len(rows_to_add),
                target_sheet_name,
            )
    elif not sync_missing_to_target:
        logger.info("Missing-company sync to target disabled by config.")

    rows_to_clear: list[dict[str, Any]] = []
    for row_num, row in enumerate(target_all[1:], start=2):
        company = row[target_company_idx].strip() if len(row) > target_company_idx else ""
        if not company:
            continue
        norm = _normalize_name(company)
        if norm not in allowed:
            rows_to_clear.append(
                {
                    "row_num": row_num,
                    "company": company,
                    "reason": "company_not_found_in_jobs_or_source",
                    "row_values": json.dumps(row, ensure_ascii=False),
                }
            )

    logger.info("Target rows with stale companies to clear: %s", len(rows_to_clear))

    if not rows_to_clear:
        logger.info("Nothing to clear. Exiting.")
        return

    _write_backup_csv(backup_csv, rows_to_clear)
    logger.info("Backup snapshot written: %s", backup_csv)

    if dry_run:
        logger.info("Dry-run enabled. No sheet rows were modified.")
        return

    for item in rows_to_clear:
        row_num = item["row_num"]
        a1 = f"A{row_num}:{target_end_col}{row_num}"
        blank_row = [[""] * target_width]

        _sheets_call(
            target_ws.update,
            blank_row,
            a1,
            value_input_option="USER_ENTERED",
        )
        _sheets_call(
            target_ws.format,
            a1,
            {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        )
        logger.debug("Cleared and whitened target row %s (%s)", row_num, item["company"])

    logger.info("Cleanup complete. Cleared %s rows in '%s'.", len(rows_to_clear), target_sheet_name)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear stale company rows from target sheet using Jobs + Company tabs",
    )
    parser.add_argument(
        "--config",
        default="cleanup_validation_config.json",
        help="Path to cleanup config (default: cleanup_validation_config.json)",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = _load_json(args.config)

    # This tool carries its own JSON config, so the project has to be looked up
    # from the main config's control block and pushed into the right place.
    #
    # It resolves the project even when none was named, exactly like every
    # other tool. It used to fall through to the spreadsheet id hardcoded in
    # the JSON, which meant a scheduled run — where no project is given —
    # DELETED ROWS from whichever sheet that file happened to name, no matter
    # which project the rest of the run was working on.
    project = projects_registry.lookup(args.project)
    if project:
        sheets = cfg["cleanup_validation"]["google_sheets"]
        configured = (sheets.get("spreadsheet_id") or "").strip()
        if configured and configured != project["spreadsheet_id"]:
            # Two answers to "which sheet" and no way to tell which was meant.
            # This step deletes rows, so it stops rather than picking one.
            sys.exit(
                f"refusing to run: --config names one spreadsheet and project "
                f"{project['id']!r} another. Clear spreadsheet_id from "
                f"{args.config} so the project decides, or run with the "
                f"matching --project.")
        sheets["spreadsheet_id"] = project["spreadsheet_id"]

    setup_logging_from_config(cfg)
    logger.info(
        "Logger initialized from config | level=%s",
        logging.getLevelName(logging.getLogger().level),
    )
    run_cleanup(cfg)


if __name__ == "__main__":
    main()
