import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class LinkedInScraper(BaseScraper):
    """Best-effort LinkedIn public jobs scraper (guest endpoint)."""

    BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)

    def fetch_jobs(self, keyword: str) -> list:
        settings  = self.config.get("scraping", {}).get("platform_settings", {}).get("linkedin", {})
        location  = str(settings.get("location", "United States")).strip()
        max_pages = max(1, int(settings.get("max_pages", 5)))
        # LinkedIn's guest "See More Jobs" endpoint returns ~10 jobs per fetch and
        # treats `start` as a true row offset. Stepping `start` by anything larger
        # than the batch size skips the rows in between, so the offset MUST match
        # the page size (default 10) to collect every job without gaps.
        page_size = max(1, int(settings.get("page_size", 10)))
        # Per-page delay (in addition to the global delay_between_requests_seconds)
        page_delay = float(settings.get("page_delay_seconds", 2.0))
        global_delay = float(self.config.get("http", {}).get("delay_between_requests_seconds", 0))

        jobs = []
        for page in range(max_pages):
            start = page * page_size
            params = {
                "keywords": keyword,
                "location": location,
                "start":    start,
            }

            logger.info(
                f"[LinkedIn] keyword='{keyword}' | page {page + 1}/{max_pages} | start={start}"
            )

            try:
                html = get_html(self._session, self.BASE_URL, self.config, params=params)
            except requests.exceptions.RequestException as exc:
                logger.error(f"[LinkedIn] Request failed for '{keyword}' page {page + 1}: {exc}")
                break

            page_jobs = self._parse(html, keyword)
            jobs.extend(page_jobs)

            logger.info(f"[LinkedIn] page {page + 1}: {len(page_jobs)} jobs (total so far: {len(jobs)})")

            # Stop early if the page returned nothing — no more results exist
            if not page_jobs:
                logger.info(f"[LinkedIn] Empty page — stopping pagination for '{keyword}'")
                break

            # Respect both the per-page delay and the global request delay
            sleep_sec = max(page_delay, global_delay)
            if sleep_sec > 0 and page < max_pages - 1:
                time.sleep(sleep_sec)

        logger.info(f"[LinkedIn] '{keyword}': {len(jobs)} total jobs across {min(page + 1, max_pages)} pages")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select("li")
        for card in cards:
            title_el = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            location_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")

            if not link_el:
                continue

            href = link_el.get("href", "")
            jobs.append(
                {
                    "Company": company_el.get_text(strip=True) if company_el else "",
                    "Role": title_el.get_text(strip=True) if title_el else "",
                    "Location": location_el.get_text(strip=True) if location_el else "",
                    "Platform": "LinkedIn",
                    "Keyword": keyword,
                    "Job Link": urljoin("https://www.linkedin.com", href) if href else "",
                }
            )

        return jobs
