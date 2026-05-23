import logging
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class GlassdoorScraper(BaseScraper):
    """Best-effort Glassdoor scraper using public search pages."""

    BASE_URL = "https://www.glassdoor.com/Job/jobs.htm"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)
        self._blocked = False

    def fetch_jobs(self, keyword: str) -> list:
        if self._blocked:
            logger.warning("[Glassdoor] Skipping: security page detected")
            return []

        settings = self.config.get("scraping", {}).get("platform_settings", {}).get("glassdoor", {})
        location = str(settings.get("location", "United States")).strip()
        max_pages = max(1, int(settings.get("max_pages", 1)))
        delay = float(self.config.get("http", {}).get("delay_between_requests_seconds", 0))

        jobs = []
        for page in range(max_pages):
            params = {
                "sc.keyword": keyword,
                "locT": "N",
                "locId": settings.get("loc_id", ""),
                "srs": "RECENT_SEARCHES",
                "p": page + 1,
            }

            logger.info(
                f"[Glassdoor] GET {self.BASE_URL} | keyword='{keyword}' | "
                f"location='{location}' | page={page + 1}"
            )

            try:
                html = get_html(self._session, self.BASE_URL, self.config, params=params)
            except requests.exceptions.RequestException as exc:
                if "403" in str(exc):
                    self._blocked = True
                logger.error(f"[Glassdoor] Request failed for '{keyword}': {exc}")
                continue

            if "Security | Glassdoor" in html or "captcha" in html.lower():
                self._blocked = True
                logger.warning("[Glassdoor] Security challenge detected; skipping further requests")
                break

            page_jobs = self._parse(html, keyword)
            jobs.extend(page_jobs)

            if delay > 0:
                time.sleep(delay)

        logger.info(f"[Glassdoor] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select("li.react-job-listing") or soup.select("article[data-test='jobListing']")
        for card in cards:
            title_el = card.select_one("a[data-test='job-link']") or card.select_one("a.jobLink")
            company_el = card.select_one("div[data-test='employer-name']") or card.select_one("span.employer-name")
            location_el = card.select_one("div[data-test='job-location']") or card.select_one("span.loc")

            if not title_el:
                continue

            href = title_el.get("href", "")
            jobs.append(
                {
                    "Company": company_el.get_text(strip=True) if company_el else "",
                    "Role": title_el.get_text(strip=True),
                    "Location": location_el.get_text(strip=True) if location_el else "",
                    "Platform": "Glassdoor",
                    "Keyword": keyword,
                    "Job Link": urljoin("https://www.glassdoor.com", href) if href else "",
                }
            )

        return jobs
