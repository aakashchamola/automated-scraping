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
        settings = self.config.get("platform_settings", {}).get("linkedin", {})
        location = str(settings.get("location", "United States")).strip()
        max_pages = max(1, int(settings.get("max_pages", 1)))
        delay = float(self.config.get("request", {}).get("delay_between_requests", 0))

        jobs = []
        for page in range(max_pages):
            params = {
                "keywords": keyword,
                "location": location,
                "start": page * 25,
            }

            logger.info(
                f"[LinkedIn] GET {self.BASE_URL} | keyword='{keyword}' | "
                f"location='{location}' | start={params['start']}"
            )

            try:
                html = get_html(self._session, self.BASE_URL, self.config, params=params)
            except requests.exceptions.RequestException as exc:
                logger.error(f"[LinkedIn] Request failed for '{keyword}': {exc}")
                continue

            page_jobs = self._parse(html, keyword)
            jobs.extend(page_jobs)

            if delay > 0:
                time.sleep(delay)

        logger.info(f"[LinkedIn] Found {len(jobs)} jobs for '{keyword}'")
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
