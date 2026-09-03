"""
main.py — Job scraping pipeline entry point.

Usage:
    python main.py                          # uses config.yaml
    python main.py --config config.yaml     # explicit config file

Adding a new platform:
    1. Create scrapers/<platform>.py with a class subclassing BaseScraper
    2. Import it here and add it to SCRAPERS
    3. Add the platform name to config.yaml under scraping.platforms
"""

import argparse
import logging
import sys

import pandas as pd

import storage
import projects_registry
import remote_store
from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
from scrapers.glassdoor import GlassdoorScraper
from scrapers.internshala import InternshalaScraper
from scrapers.lever import LeverScraper
from scrapers.linkedin import LinkedInScraper
from logger_setup import setup_logging_from_config
from scrapers.indeed import IndeedScraper
from scrapers.simplyhired import SimplyHiredScraper
from scrapers.wellfound import WellfoundScraper
from scrapers.ycombinator import YCombinatorScraper

# ── Platform registry ────────────────────────────────────────────────────────
# Add new scrapers here: "platform_name": ScraperClass
SCRAPERS = {
    "indeed": IndeedScraper,
    "linkedin": LinkedInScraper,
    "glassdoor": GlassdoorScraper,
    "jobs.lever": LeverScraper,
    "lever": LeverScraper,
    "internshala": InternshalaScraper,
    "internshaala": InternshalaScraper,
    "wellfound": WellfoundScraper,
    "angellist": WellfoundScraper,
    "ycombinator": YCombinatorScraper,
    "simplyhired": SimplyHiredScraper,
}

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def validate_config(config: dict) -> dict:
    """Apply defaults for all optional config sections so downstream code
    can always do simple .get() without checking for key existence."""
    scraping = config.setdefault("scraping", {})
    scraping.setdefault("output_csv", "jobs.csv")
    scraping.setdefault("keywords_fallback_file", "keywords.txt")
    scraping.setdefault("keywords_source", {})
    scraping.setdefault("platforms", [])
    scraping.setdefault("platform_settings", {})

    if not isinstance(scraping.get("platforms"), list):
        logger.error("Config key 'scraping.platforms' must be a list")
        sys.exit(1)

    http = config.setdefault("http", {})
    http.setdefault("timeout_seconds", 10)
    http.setdefault("max_retries", 3)
    http.setdefault("retry_delay_seconds", 1)
    http.setdefault("delay_between_requests_seconds", 0)

    config.setdefault("career_pages", {})
    config.setdefault("job_validation", {})
    config.setdefault("logging", {}).setdefault("level", "info")

    gs = config.setdefault("google_sheets", {})
    gs.setdefault("enabled", False)
    gs.setdefault("credentials_file", "")
    gs.setdefault("spreadsheet_id", "")
    gs.setdefault("jobs_worksheet", "Jobs")

    return config


def load_keywords(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            keywords = [line.strip() for line in fh if line.strip()]
        if not keywords:
            logger.warning(f"No keywords found in '{path}'")
        return keywords
    except FileNotFoundError:
        logger.error(f"Keywords file not found: '{path}'")
        sys.exit(1)


def resolve_keywords(config: dict) -> list:
    """Load keywords from the Keywords sheet when configured, else the txt file.

    config.scraping.keywords_source.mode = "sheet" | "file"
    Falls back to the txt file if the sheet yields nothing.
    """
    scraping = config.get("scraping", {})
    src = scraping.get("keywords_source", {})
    fallback_file = scraping.get("keywords_fallback_file", "keywords.txt")

    if src.get("mode") == "sheet":
        worksheet = src.get("worksheet", "Keywords")
        column = src.get("column", "Search Term")
        store = remote_store.store_for(config)
        try:
            keywords = store.load_column_values(column, worksheet)
        except Exception as exc:
            logger.error(f"Failed to read keywords from sheet '{worksheet}': {exc}")
            keywords = []
        if keywords:
            logger.info(
                f"Loaded {len(keywords)} keywords from sheet '{worksheet}' "
                f"column '{column}'"
            )
            return keywords
        logger.warning(
            f"No keywords from sheet '{worksheet}'; falling back to '{fallback_file}'"
        )
    return load_keywords(fallback_file)


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run(config: dict) -> None:
    scraping = config.get("scraping", {})
    keywords = resolve_keywords(config)
    platforms = scraping.get("platforms", [])
    output_csv = scraping.get("output_csv", "jobs.csv")

    logger.info(
        f"Pipeline start | platforms={platforms} | keywords={keywords}"
    )

    all_new_jobs = []

    for platform_name in platforms:
        logger.info(f"Platform start: {platform_name}")
        scraper_cls = SCRAPERS.get(platform_name)
        if scraper_cls is None:
            logger.warning(
                f"No scraper registered for platform '{platform_name}'. "
                "Skipping."
            )
            continue

        try:
            scraper = scraper_cls(config)
        except Exception as exc:
            logger.error(
                f"Failed to initialize scraper '{platform_name}': {exc}"
            )
            continue

        platform_total = 0

        for keyword in keywords:
            try:
                jobs = scraper.fetch_jobs(keyword)
            except Exception as exc:
                logger.error(
                    f"Unhandled error scraping '{platform_name}' for "
                    f"keyword '{keyword}': {exc}"
                )
                jobs = []

            logger.info(
                f"{platform_name} | keyword='{keyword}' | collected={len(jobs)}"
            )
            all_new_jobs.extend(jobs)
            platform_total += len(jobs)

        logger.info(
            f"Platform complete: {platform_name} | total_collected={platform_total}"
        )

    if not all_new_jobs:
        logger.warning("No jobs were collected this run. Exiting without writing.")
        return

    new_df = pd.DataFrame(all_new_jobs)
    existing_csv_df = storage.load_existing(output_csv)

    # Which store this is depends on how the machine is set up: the Web App
    # when it has a project password and no Google key, the Sheets API when it
    # has the key. Nothing below can tell the difference.
    sheet_store = remote_store.store_for(config)
    existing_sheet_df = pd.DataFrame(columns=storage.OUTPUT_COLUMNS)
    if sheet_store.is_enabled():
        try:
            existing_sheet_df = sheet_store.load_existing()
        except Exception as exc:
            logger.exception(f"Failed to read Google Sheets data: {exc!r}")
            sys.exit(1)

    existing_combined_df = pd.concat(
        [existing_csv_df, existing_sheet_df],
        ignore_index=True,
    )

    merged_df = storage.deduplicate(new_df, existing_combined_df)
    storage.save(merged_df, output_csv)

    if sheet_store.is_enabled():
        rows_to_append = storage.get_new_rows(new_df, existing_combined_df)
        # Hyperlink the Company cell to its LinkedIn URL when the company is in
        # our Company database sheet.
        cs = config.get("career_pages", {})
        try:
            company_linkedin = sheet_store.load_company_linkedin_map(
                worksheet_name=cs.get("source_worksheet", "Company"),
                company_col=cs.get("company_column", "Company"),
            )
        except Exception as exc:
            logger.warning(f"Could not load company LinkedIn map: {exc}")
            company_linkedin = {}
        try:
            sheet_store.append_rows(rows_to_append, company_linkedin=company_linkedin)
        except Exception as exc:
            logger.exception(f"Failed to append to Google Sheets: {exc!r}")
            sys.exit(1)

    logger.info(
        f"Pipeline complete. Total records: {len(merged_df)} in '{output_csv}'"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job scraping pipeline")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML or JSON config file (default: config.yaml)",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = load_config(args.config)
    projects_registry.resolve(cfg, args.project)
    setup_logging_from_config(cfg)
    logger.info(
        "Logger initialized from config | level=%s",
        logging.getLevelName(logging.getLogger().level),
    )
    cfg = validate_config(cfg)
    run(cfg)
