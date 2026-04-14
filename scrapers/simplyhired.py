import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class SimplyHiredScraper(BaseScraper):
    """Best-effort SimplyHired scraper."""

    BASE_URL = "https://www.simplyhired.com/search"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)

    def fetch_jobs(self, keyword: str) -> list:
        settings = self.config.get("platform_settings", {}).get("simplyhired", {})
        location = str(settings.get("location", "United States")).strip()
        max_pages = max(1, int(settings.get("max_pages", 1)))
        delay = float(self.config.get("request", {}).get("delay_between_requests", 0))

        jobs = []
        for page in range(max_pages):
            params = {
                "q": keyword,
                "l": location,
                "pn": page + 1,
            }

            logger.info(
                f"[SimplyHired] GET {self.BASE_URL} | keyword='{keyword}' | "
                f"location='{location}' | page={page + 1}"
            )

            try:
                html = get_html(self._session, self.BASE_URL, self.config, params=params)
            except requests.exceptions.RequestException as exc:
                logger.error(f"[SimplyHired] Request failed for '{keyword}': {exc}")
                continue

            page_jobs = self._parse(html, keyword)
            jobs.extend(page_jobs)

            if delay > 0:
                time.sleep(delay)

        logger.info(f"[SimplyHired] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select("div[data-testid='searchSerpJob']") or soup.select("div.SerpJob-jobCard")
        for card in cards:
            title_el = card.select_one("a[data-testid='jobTitle']") or card.select_one("a[href*='/job/']")
            company_el = card.select_one("span[data-testid='companyName']")
            location_el = card.select_one("span[data-testid='searchSerpJobLocation']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            jobs.append(
                {
                    "Company": company_el.get_text(strip=True) if company_el else "",
                    "Role": title_el.get_text(strip=True),
                    "Location": location_el.get_text(strip=True) if location_el else "",
                    "Platform": "SimplyHired",
                    "Keyword": keyword,
                    "Job Link": urljoin("https://www.simplyhired.com", href) if href else "",
                }
            )

        return jobs
