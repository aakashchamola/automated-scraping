import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class WellfoundScraper(BaseScraper):
    """Best-effort Wellfound (AngelList) jobs scraper."""

    BASE_URL = "https://wellfound.com/jobs"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)
        self._blocked = False

    def fetch_jobs(self, keyword: str) -> list:
        if self._blocked:
            logger.warning("[Wellfound] Skipping: access blocked by anti-bot checks")
            return []

        settings = self.config.get("platform_settings", {}).get("wellfound", {})
        location = str(settings.get("location", "United States")).strip()
        delay = float(self.config.get("request", {}).get("delay_between_requests", 0))

        params = {
            "query": keyword,
            "location": location,
        }

        logger.info(f"[Wellfound] GET {self.BASE_URL} | keyword='{keyword}' | location='{location}'")
        try:
            html = get_html(self._session, self.BASE_URL, self.config, params=params)
        except requests.exceptions.RequestException as exc:
            if "403" in str(exc):
                self._blocked = True
            logger.error(f"[Wellfound] Request failed for '{keyword}': {exc}")
            return []

        jobs = self._parse(html, keyword)

        if delay > 0:
            time.sleep(delay)

        logger.info(f"[Wellfound] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select("div[data-testid='StartupResult']") or soup.select("div.styles_component__")
        for card in cards:
            title_el = card.select_one("a[data-testid='job-title']") or card.select_one("a[href*='/jobs/']")
            company_el = card.select_one("a[data-testid='startup-name']")
            location_el = card.select_one("span[data-testid='job-location']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            jobs.append(
                {
                    "Company": company_el.get_text(strip=True) if company_el else "",
                    "Role": title_el.get_text(strip=True),
                    "Location": location_el.get_text(strip=True) if location_el else "",
                    "Platform": "Wellfound",
                    "Keyword": keyword,
                    "Job Link": urljoin("https://wellfound.com", href) if href else "",
                }
            )

        return jobs
