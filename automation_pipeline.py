"""
automation_pipeline.py — Master automation runner.

Runs the automation tasks in sequence:

    1. Company enrichment   (fill employee count / career page / LinkedIn URL)
    2. Career-page scraping  (scrape jobs from company career pages)
    3. Keyword scraping      (LinkedIn, Indeed, Glassdoor, ...)
    4. Job validation        (mark Active / Removed / Expired / Unknown)

Each step can be skipped. Career-page jobs are deduplicated against existing
rows using the same storage logic the keyword pipeline uses, then appended to
the configured Jobs worksheet.

Usage:
    python automation_pipeline.py --config config.json
    python automation_pipeline.py --config config.json --skip-enrichment
    python automation_pipeline.py --config config.json --only-validation
"""

import argparse
import logging
import sys

import pandas as pd

import company_enricher
import job_validator
import projects_registry
from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
from logger_setup import setup_logging_from_config
from main import run as keyword_scraping_run, resolve_keywords, validate_config
from scrapers.career_page import scrape_companies

logger = logging.getLogger(__name__)


def _rows_to_dicts(rows: list) -> list:
    """Convert raw sheet rows (list of lists, first row = header) to dicts."""
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def run_enrichment(config: dict) -> None:
    logger.info("=" * 70)
    logger.info("STEP 1: Company enrichment")
    logger.info("=" * 70)
    gs_config = config.get("google_sheets", {})
    companies_worksheet = gs_config.get("enrichment_output_worksheet", "CompaniesTest")
    company_enricher.enrich(gs_config, companies_worksheet)
    logger.info("Company enrichment complete")


def run_career_page_scraping(config: dict) -> None:
    logger.info("=" * 70)
    logger.info("STEP 2: Career-page scraping")
    logger.info("=" * 70)
    gs_config = config.get("google_sheets", {})
    if not gs_config.get("enabled"):
        logger.warning("Google Sheets not enabled; skipping career-page scraping")
        return

    store = GoogleSheetsStore(gs_config)

    # Career pages come from the manually-curated Company sheet.
    cs = config.get("career_pages", {})
    source_ws = cs.get("source_worksheet", "Company")
    company_col = cs.get("company_column", "Company")
    career_col = cs.get("career_page_column", "Career-Page")
    max_jobs = int(cs.get("max_jobs_per_company", 50))

    rows = store.load_all_rows(source_ws)
    companies = _rows_to_dicts(rows)
    if not companies:
        logger.warning(f"No companies in '{source_ws}'")
        return

    # Career-page jobs must be relevant to our keywords (same list the
    # keyword scrapers use).
    keywords = resolve_keywords(config)
    logger.info(
        f"Scraping career pages for {len(companies)} companies in '{source_ws}' "
        f"| {len(keywords)} keywords | career column '{career_col}'"
    )
    jobs = scrape_companies(
        companies,
        keywords=keywords,
        company_col=company_col,
        career_col=career_col,
        max_jobs=max_jobs,
        match_mode=cs.get("keyword_match_mode", "all"),
    )
    if not jobs:
        logger.warning("No keyword-matched jobs found from career pages")
        return

    # Schema-aware dedup: drop jobs whose Job Link already exists in the sheet.
    jobs_worksheet = gs_config.get("jobs_worksheet", "Jobs")
    existing_links = _existing_job_links(store, jobs_worksheet)
    new_jobs = [j for j in jobs if j.get("Job Link", "") not in existing_links]
    logger.info(
        f"Career jobs: {len(jobs)} scraped, {len(jobs) - len(new_jobs)} duplicates, "
        f"{len(new_jobs)} new"
    )
    if not new_jobs:
        logger.info("All career-page jobs already present; nothing to append")
        return

    # Hyperlink each Company cell to its LinkedIn URL (from the Company sheet).
    company_linkedin = store.load_company_linkedin_map(
        worksheet_name=source_ws, company_col=company_col, linkedin_col="Linkedin-Url"
    )
    store.append_rows(pd.DataFrame(new_jobs), company_linkedin=company_linkedin)
    logger.info(f"Appended {len(new_jobs)} new career-page jobs to '{jobs_worksheet}'")


def _existing_job_links(store: GoogleSheetsStore, worksheet: str) -> set:
    """Return the set of Job Link values already present in ``worksheet``."""
    rows = store.load_all_rows(worksheet)
    if not rows:
        return set()
    header = rows[0]
    if "Job Link" not in header:
        return set()
    idx = header.index("Job Link")
    return {
        r[idx].strip()
        for r in rows[1:]
        if idx < len(r) and r[idx].strip()
    }


def run_keyword_scraping(config: dict) -> None:
    logger.info("=" * 70)
    logger.info("STEP 3: Keyword-based scraping")
    logger.info("=" * 70)
    keyword_scraping_run(config)
    logger.info("Keyword scraping complete")


def run_validation(config: dict) -> None:
    logger.info("=" * 70)
    logger.info("STEP 4: Job validation")
    logger.info("=" * 70)
    gs_config = config.get("google_sheets", {})
    if not gs_config.get("enabled"):
        logger.warning("Google Sheets not enabled; skipping validation")
        return
    store = GoogleSheetsStore(gs_config)
    job_validator.validate_jobs(
        store,
        worksheet=gs_config.get("jobs_worksheet", "Jobs"),
        job_url_column="Job Link",
        status_column="Job Status",
    )
    logger.info("Job validation complete")


def run_pipeline(
    config: dict,
    skip_enrichment: bool = False,
    skip_career: bool = False,
    skip_keyword: bool = False,
    skip_validation: bool = False,
) -> None:
    logger.info("AUTOMATION PIPELINE START")

    steps = [
        ("enrichment", skip_enrichment, run_enrichment),
        ("career-page scraping", skip_career, run_career_page_scraping),
        ("keyword scraping", skip_keyword, run_keyword_scraping),
        ("validation", skip_validation, run_validation),
    ]

    for name, skip, fn in steps:
        if skip:
            logger.info(f"Skipped: {name}")
            continue
        try:
            fn(config)
        except Exception as exc:
            logger.exception(f"Step '{name}' failed: {exc!r}")
            sys.exit(1)

    logger.info("AUTOMATION PIPELINE COMPLETE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Master automation pipeline")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--skip-career-scraping", action="store_true")
    parser.add_argument("--skip-keyword-scraping", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument(
        "--only-validation",
        action="store_true",
        help="Shortcut: run validation only (skip the other three steps)",
    )
    parser.add_argument(
        "--only-career",
        action="store_true",
        help="Shortcut: run career-page scraping only",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = load_config(args.config)
    projects_registry.resolve(cfg, args.project)
    setup_logging_from_config(cfg, name="pipeline")
    cfg = validate_config(cfg)

    skip_enrichment = args.skip_enrichment
    skip_career = args.skip_career_scraping
    skip_keyword = args.skip_keyword_scraping
    skip_validation = args.skip_validation

    if args.only_validation:
        skip_enrichment = skip_career = skip_keyword = True
        skip_validation = False
    if args.only_career:
        skip_enrichment = skip_keyword = skip_validation = True
        skip_career = False

    run_pipeline(
        cfg,
        skip_enrichment=skip_enrichment,
        skip_career=skip_career,
        skip_keyword=skip_keyword,
        skip_validation=skip_validation,
    )
