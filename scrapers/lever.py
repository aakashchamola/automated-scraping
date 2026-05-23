import logging
import time

import requests

from scrapers import BaseScraper
from scrapers.http_utils import build_session

logger = logging.getLogger(__name__)


class LeverScraper(BaseScraper):
    """Scraper for Lever postings API using configured company site slugs."""

    BASE_URL = "https://api.lever.co/v0/postings/{site}"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)

    def fetch_jobs(self, keyword: str) -> list:
        settings = self.config.get("scraping", {}).get("platform_settings", {}).get("lever", {})
        location = str(settings.get("location", "United States")).strip()
        sites = settings.get("sites", [])
        delay = float(self.config.get("http", {}).get("delay_between_requests_seconds", 0))

        if not sites:
            logger.warning(
                "[Lever] No company sites configured. Add platform_settings.lever.sites "
                "in config.json to enable Lever scraping."
            )
            return []

        jobs = []
        for site in sites:
            url = self.BASE_URL.format(site=site)
            params = {"mode": "json"}

            logger.info(f"[Lever] GET {url} | site='{site}' | keyword='{keyword}'")

            try:
                response = self._session.get(
                    url,
                    params=params,
                    timeout=self.config.get("http", {}).get("timeout_seconds", 10),
                )
                response.raise_for_status()
                postings = response.json()
            except requests.exceptions.RequestException as exc:
                logger.error(f"[Lever] Request failed for site '{site}': {exc}")
                continue
            except ValueError as exc:
                logger.error(f"[Lever] Invalid JSON for site '{site}': {exc}")
                continue

            site_jobs = self._parse(postings, keyword, location)
            jobs.extend(site_jobs)

            if delay > 0:
                time.sleep(delay)

        logger.info(f"[Lever] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, postings: list, keyword: str, location_filter: str) -> list:
        jobs = []
        keyword_l = keyword.lower()
        location_l = location_filter.lower()

        for posting in postings:
            title = str(posting.get("text", "")).strip()
            company = str(posting.get("categories", {}).get("team", "")).strip()
            location = str(posting.get("categories", {}).get("location", "")).strip()
            link = str(posting.get("hostedUrl", "")).strip()

            haystack = f"{title} {company} {location}".lower()
            if keyword_l and keyword_l not in haystack:
                continue
            if location_l and location_l not in location.lower() and location_l != "united states":
                continue

            jobs.append(
                {
                    "Company": company,
                    "Role": title,
                    "Location": location,
                    "Platform": "jobs.lever",
                    "Keyword": keyword,
                    "Job Link": link,
                }
            )

        return jobs
