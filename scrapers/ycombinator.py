import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class YCombinatorScraper(BaseScraper):
    """Best-effort Y Combinator Work at a Startup jobs scraper."""

    BASE_URL = "https://www.workatastartup.com/jobs"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)

    def fetch_jobs(self, keyword: str) -> list:
        settings = self.config.get("scraping", {}).get("platform_settings", {}).get("ycombinator", {})
        location = str(settings.get("location", "United States")).strip()
        delay = float(self.config.get("http", {}).get("delay_between_requests_seconds", 0))

        params = {
            "query": keyword,
            "location": location,
        }

        logger.info(
            f"[YCombinator] GET {self.BASE_URL} | keyword='{keyword}' | location='{location}'"
        )
        try:
            html = get_html(self._session, self.BASE_URL, self.config, params=params)
        except requests.exceptions.RequestException as exc:
            logger.error(f"[YCombinator] Request failed for '{keyword}': {exc}")
            return []

        jobs = self._parse(html, keyword)

        if delay > 0:
            time.sleep(delay)

        logger.info(f"[YCombinator] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select("a[href*='/jobs/']")
        for card in cards:
            title_text = card.get_text(" ", strip=True)
            href = card.get("href", "")

            if not href or not title_text:
                continue

            jobs.append(
                {
                    "Company": "",
                    "Role": title_text,
                    "Location": "",
                    "Platform": "Y Combinator",
                    "Keyword": keyword,
                    "Job Link": urljoin("https://www.workatastartup.com", href),
                }
            )

        return jobs
