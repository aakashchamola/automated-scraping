"""
main.py — Job scraping pipeline entry point.

Usage:
    python main.py                        # uses config.json
    python main.py --config my_cfg.json   # custom config file

Adding a new platform:
    1. Create scrapers/<platform>.py with a class subclassing BaseScraper
    2. Import it here and add it to SCRAPERS
    3. Add the platform name to config.json "platforms" list
"""

import argparse
import json
import logging
import sys

import pandas as pd

import storage
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

def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.error(f"Config file not found: '{path}'")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error(f"Config file is not valid JSON: {exc}")
        sys.exit(1)


def validate_config(config: dict) -> dict:
    required = ["output_file", "keywords_file", "platforms"]
    missing = [key for key in required if key not in config]
    if missing:
        logger.error(f"Missing required config keys: {missing}")
        sys.exit(1)

    if not isinstance(config.get("platforms"), list):
        logger.error("Config key 'platforms' must be a list")
        sys.exit(1)

    request_cfg = config.setdefault("request", {})
    request_cfg.setdefault("timeout", 10)
    request_cfg.setdefault("max_retries", 3)
    request_cfg.setdefault("retry_delay", 1)
    request_cfg.setdefault("delay_between_requests", 0)

    config.setdefault("platform_settings", {})
    logging_cfg = config.setdefault("logging", {})
    logging_cfg.setdefault("level", "info")
    gs_cfg = config.setdefault("google_sheets", {})
    gs_cfg.setdefault("enabled", False)
    gs_cfg.setdefault("credentials_file", "")
    gs_cfg.setdefault("spreadsheet_id", "")
    gs_cfg.setdefault("worksheet", "Jobs")
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


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run(config: dict) -> None:
    keywords = load_keywords(config["keywords_file"])
    platforms = config.get("platforms", [])

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

    existing_csv_df = storage.load_existing(config["output_file"])

    sheet_store = GoogleSheetsStore(config.get("google_sheets", {}))
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
    storage.save(merged_df, config["output_file"])

    if sheet_store.is_enabled():
        rows_to_append = storage.get_new_rows(new_df, existing_combined_df)
        try:
            sheet_store.append_rows(rows_to_append)
        except Exception as exc:
            logger.exception(f"Failed to append to Google Sheets: {exc!r}")
            sys.exit(1)

    logger.info(
        f"Pipeline complete. Total records: {len(merged_df)} "
        f"in '{config['output_file']}'"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job scraping pipeline")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file (default: config.json)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = load_config(args.config)
    setup_logging_from_config(cfg)
    logger.info(
        "Logger initialized from config | level=%s",
        logging.getLevelName(logging.getLogger().level),
    )
    cfg = validate_config(cfg)
    run(cfg)
