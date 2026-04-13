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
from logger_setup import setup_logging
from scrapers.indeed import IndeedScraper

# ── Platform registry ────────────────────────────────────────────────────────
# Add new scrapers here: "platform_name": ScraperClass
SCRAPERS = {
    "indeed": IndeedScraper,
}

setup_logging()
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
        scraper_cls = SCRAPERS.get(platform_name)
        if scraper_cls is None:
            logger.warning(
                f"No scraper registered for platform '{platform_name}'. "
                "Skipping."
            )
            continue

        scraper = scraper_cls(config)

        for keyword in keywords:
            jobs = scraper.fetch_jobs(keyword)
            all_new_jobs.extend(jobs)

    if not all_new_jobs:
        logger.warning("No jobs were collected this run. Exiting without writing.")
        return

    new_df = pd.DataFrame(all_new_jobs)
    existing_df = storage.load_existing(config["output_file"])
    merged_df = storage.deduplicate(new_df, existing_df)
    storage.save(merged_df, config["output_file"])

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
    run(cfg)
