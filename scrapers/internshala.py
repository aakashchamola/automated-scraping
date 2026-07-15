import logging
import time
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from scrapers import BaseScraper
from scrapers.http_utils import build_session, get_html

logger = logging.getLogger(__name__)


class InternshalaScraper(BaseScraper):
    """Internshala scraper — keyword-based internship/job search."""

    BASE_URL = "https://internshala.com/internships/keywords-{}"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = build_session(config)

    def fetch_jobs(self, keyword: str) -> list:
        delay = float(self.config.get("http", {}).get("delay_between_requests_seconds", 0))
        slug = keyword.lower().replace(" ", "-")
        url = self.BASE_URL.format(quote_plus(slug))

        logger.info(f"[Internshala] GET {url} | keyword='{keyword}'")
        try:
            html = get_html(self._session, url, self.config)
        except requests.exceptions.RequestException as exc:
            logger.error(f"[Internshala] Request failed for '{keyword}': {exc}")
            return []

        jobs = self._parse(html, keyword)
        if delay > 0:
            time.sleep(delay)

        logger.info(f"[Internshala] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        for card in soup.select("div.individual_internship"):
            link_el    = card.select_one("a.job-title-href")
            company_el = card.select_one("p.company-name")
            location_el = card.select_one("div.location_link a") or card.select_one("p.locations")

            if not link_el:
                continue

            href  = link_el.get("href", "")
            title = link_el.get_text(strip=True)  # the link text IS the job title
            if not title:
                continue

            jobs.append({
                "Company":  company_el.get_text(strip=True) if company_el else "",
                "Role":     title,
                "Location": location_el.get_text(strip=True) if location_el else "",
                "Platform": "Internshala",
                "Keyword":  keyword,
                "Job Link": urljoin("https://internshala.com", href) if href else "",
            })

        return jobs
