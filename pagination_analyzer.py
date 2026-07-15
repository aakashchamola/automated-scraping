"""
pagination_analyzer.py — LinkedIn pagination / "See More Jobs" depth analysis.

Answers, for each search keyword, the two questions in Tasks #4 and #8:
  - How many jobs can be collected *beyond the first page*?
  - How deep does LinkedIn's "See More Jobs" / infinite-scroll go before the
    results run out?

LinkedIn's public "See more jobs" button and the infinite scroll on the guest
jobs search are both powered by the same endpoint the scraper already uses:

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?start=N

where ``start`` steps by 25 (one "page" == one "See more jobs" fetch). This tool
walks that endpoint page by page until it returns an empty page (no more results)
or a safety cap is reached, recording how many jobs appear on each page.

It is READ-ONLY: it never writes to the Jobs sheet. A per-keyword report is
written to ``logs/<output_csv>`` and, optionally, to a test worksheet.

Usage:
    python pagination_analyzer.py --config config.yaml
    python pagination_analyzer.py --config config.yaml --keywords "Microbiologist" --max-pages 20
    python pagination_analyzer.py --config config.yaml --limit 3      # first 3 keywords only
"""

import argparse
import csv
import logging
import os
import sys
import time

import requests

from config_loader import load_config
from logger_setup import setup_logging_from_config
from scrapers.http_utils import build_session, get_html
from scrapers.linkedin import LinkedInScraper

logger = logging.getLogger(__name__)

LINKEDIN_SEEMORE_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

REPORT_HEADER = [
    "Keyword",
    "Pages Fetched",
    "Total Jobs",
    "Page 1 Jobs",
    "Beyond Page 1",
    "Last Non-Empty Page",
    "Hit Probe Cap",
    "Recommended Max Pages",
]


def _analysis_cfg(config: dict) -> dict:
    """Read pagination_analysis settings with sane defaults."""
    pa = config.get("pagination_analysis", {}) or {}
    return {
        "location":         str(pa.get("location", "United States")).strip(),
        "page_size":        int(pa.get("page_size", 25)),
        "max_probe_pages":  max(1, int(pa.get("max_probe_pages", 20))),
        "page_delay":       float(pa.get("page_delay_seconds", 1.5)),
        "keywords_limit":   int(pa.get("keywords_limit", 0)),
        "output_csv":       str(pa.get("output_csv", "logs/pagination_analysis_latest.csv")),
        "write_to_sheet":   bool(pa.get("write_to_sheet", False)),
        "output_worksheet": str(pa.get("output_worksheet", "Pagination_Analysis")),
    }


def probe_keyword(
    session: requests.Session,
    parser: LinkedInScraper,
    config: dict,
    keyword: str,
    location: str,
    page_size: int,
    max_probe_pages: int,
    page_delay: float,
) -> dict:
    """Walk the LinkedIn 'See More Jobs' endpoint for one keyword.

    Returns a report dict (one row of REPORT_HEADER) plus a per-page list.
    """
    per_page = []          # NEW unique jobs found on each page
    seen = set()           # job links already counted (dedupe across pages)
    page1 = 0
    last_nonempty = 0
    hit_cap = False

    for page in range(max_probe_pages):
        start = page * page_size
        params = {"keywords": keyword, "location": location, "start": start}
        try:
            html = get_html(session, LINKEDIN_SEEMORE_URL, config, params=params)
        except requests.exceptions.RequestException as exc:
            logger.warning(
                f"[{keyword}] page {page + 1} (start={start}) request failed: {exc}"
            )
            break

        links = [j.get("Job Link", "") for j in parser._parse(html, keyword)]
        raw = len(links)
        new_links = [u for u in links if u and u not in seen]
        seen.update(new_links)
        new_n = len(new_links)
        per_page.append(new_n)
        if page == 0:
            page1 = raw
        logger.debug(
            f"[{keyword}] page {page + 1}/{max_probe_pages} (start={start}): "
            f"{raw} jobs ({new_n} new, {raw - new_n} dupes)"
        )

        # Stop when the page is empty OR contributes only already-seen jobs —
        # at the ceiling LinkedIn often repeats the last batch instead of 0.
        if raw == 0:
            logger.info(f"[{keyword}] empty page {page + 1} — results exhausted")
            break
        if new_n == 0:
            logger.info(f"[{keyword}] page {page + 1} all duplicates — ceiling reached")
            break

        last_nonempty = page + 1
        if page == max_probe_pages - 1:
            hit_cap = True

        if page_delay > 0 and page < max_probe_pages - 1:
            time.sleep(page_delay)

    total = len(seen)
    beyond = max(0, total - page1)
    recommended = last_nonempty if not hit_cap else max_probe_pages
    report = {
        "Keyword": keyword,
        "Pages Fetched": len(per_page),
        "Total Jobs": total,
        "Page 1 Jobs": page1,
        "Beyond Page 1": beyond,
        "Last Non-Empty Page": last_nonempty,
        "Hit Probe Cap": "yes" if hit_cap else "no",
        "Recommended Max Pages": recommended,
    }
    logger.info(
        f"[{keyword}] total={total} jobs across {last_nonempty} non-empty pages "
        f"| page1={page1} | beyond_page1={beyond} | hit_cap={report['Hit Probe Cap']}"
    )
    return report


def analyze(config: dict, keywords: list) -> list:
    """Probe every keyword and return a list of report dicts."""
    settings = _analysis_cfg(config)
    if settings["keywords_limit"] > 0:
        keywords = keywords[: settings["keywords_limit"]]

    session = build_session(config)
    parser = LinkedInScraper(config)   # reuse the production _parse() for consistent counts

    logger.info(
        f"Pagination analysis | {len(keywords)} keywords | page_size="
        f"{settings['page_size']} | max_probe_pages={settings['max_probe_pages']}"
    )

    reports = []
    for i, kw in enumerate(keywords, start=1):
        logger.info(f"--- Keyword {i}/{len(keywords)}: {kw!r} ---")
        reports.append(
            probe_keyword(
                session, parser, config, kw,
                location=settings["location"],
                page_size=settings["page_size"],
                max_probe_pages=settings["max_probe_pages"],
                page_delay=settings["page_delay"],
            )
        )

    _write_csv(reports, settings["output_csv"])
    _log_summary(reports, settings["page_size"])

    if settings["write_to_sheet"]:
        _write_sheet(config, reports, settings["output_worksheet"])

    return reports


def _write_csv(reports: list, path: str) -> None:
    if not reports:
        logger.warning("No reports to write")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_HEADER)
        writer.writeheader()
        writer.writerows(reports)
    logger.info(f"Wrote pagination report to '{path}'")


def _log_summary(reports: list, page_size: int) -> None:
    if not reports:
        return
    total_jobs = sum(r["Total Jobs"] for r in reports)
    total_page1 = sum(r["Page 1 Jobs"] for r in reports)
    total_beyond = sum(r["Beyond Page 1"] for r in reports)
    capped = sum(1 for r in reports if r["Hit Probe Cap"] == "yes")
    deepest = max(reports, key=lambda r: r["Last Non-Empty Page"])
    pct = (100.0 * total_beyond / total_jobs) if total_jobs else 0.0
    logger.info("=" * 70)
    logger.info("PAGINATION ANALYSIS SUMMARY")
    logger.info(f"  Keywords analyzed     : {len(reports)}")
    logger.info(f"  Page size (per fetch) : {page_size} jobs")
    logger.info(f"  Total jobs reachable  : {total_jobs}")
    logger.info(f"  On page 1             : {total_page1}")
    logger.info(
        f"  Beyond page 1         : {total_beyond}  "
        f"({pct:.0f}% of all jobs are behind 'See More Jobs')"
    )
    logger.info(
        f"  Deepest keyword       : {deepest['Keyword']!r} → "
        f"{deepest['Last Non-Empty Page']} pages "
        f"({deepest['Total Jobs']} jobs)"
    )
    logger.info(f"  Keywords that hit cap : {capped} (more jobs may exist beyond cap)")
    logger.info("=" * 70)


def _write_sheet(config: dict, reports: list, worksheet: str) -> None:
    """Write the report to a (test) worksheet, replacing its contents."""
    from google_sheets_store import GoogleSheetsStore

    gs = config.get("google_sheets", {})
    if not gs.get("enabled"):
        logger.warning("Google Sheets not enabled; skipping sheet write")
        return
    store = GoogleSheetsStore(gs)
    ws = store.open_worksheet(worksheet)
    rows = [REPORT_HEADER] + [[str(r[c]) for c in REPORT_HEADER] for r in reports]
    ws.clear()
    ws.update("A1", rows)
    logger.info(f"Wrote {len(reports)} report rows to worksheet '{worksheet}'")


def _resolve_keywords(config: dict, cli_keywords: str) -> list:
    if cli_keywords:
        return [k.strip() for k in cli_keywords.split(",") if k.strip()]
    # Reuse the same keyword resolution the scraper uses (Keywords sheet / file).
    from main import resolve_keywords
    return resolve_keywords(config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze LinkedIn pagination / 'See More Jobs' depth per keyword"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--keywords",
        default="",
        help="Comma-separated keywords to probe (default: from Keywords sheet/file)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Override pagination_analysis.max_probe_pages (0 = use config)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Probe only the first N keywords (0 = use config keywords_limit)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    setup_logging_from_config(config, name="pagination")

    # CLI overrides into the config dict so _analysis_cfg picks them up.
    pa = config.setdefault("pagination_analysis", {})
    if args.max_pages > 0:
        pa["max_probe_pages"] = args.max_pages
    if args.limit > 0:
        pa["keywords_limit"] = args.limit

    keywords = _resolve_keywords(config, args.keywords)
    if not keywords:
        logger.error("No keywords to analyze")
        sys.exit(1)

    analyze(config, keywords)
