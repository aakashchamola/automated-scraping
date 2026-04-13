import logging

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scrapers import BaseScraper

logger = logging.getLogger(__name__)

# CSS selectors — update these if Indeed changes its markup
_SELECTORS = {
    "card": ".job_seen_beacon",
    "title": "h2.jobTitle",
    "company": '[data-testid="company-name"], .companyName',
    "location": '[data-testid="text-location"], .companyLocation',
    "link": "a[id^='job_']",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class IndeedScraper(BaseScraper):
    """Scraper for in.indeed.com."""

    BASE_URL = "https://in.indeed.com/jobs?q={}&l="

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._session = self._build_session()

    # ------------------------------------------------------------------
    # Session with automatic retry on transient HTTP errors
    # ------------------------------------------------------------------
    def _build_session(self) -> requests.Session:
        req_cfg = self.config.get("request", {})
        retry = Retry(
            total=req_cfg.get("max_retries", 3),
            backoff_factor=req_cfg.get("retry_delay", 2),
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def fetch_jobs(self, keyword: str) -> list:
        url = self.BASE_URL.format(keyword.replace(" ", "+"))
        logger.info(f"[Indeed] GET {url}")

        try:
            response = self._session.get(
                url,
                headers=_HEADERS,
                timeout=self.config.get("request", {}).get("timeout", 10),
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            logger.error(f"[Indeed] HTTP error for '{keyword}': {exc}")
            return []
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"[Indeed] Connection error for '{keyword}': {exc}")
            return []
        except requests.exceptions.Timeout:
            logger.error(f"[Indeed] Request timed out for '{keyword}'")
            return []
        except requests.exceptions.RequestException as exc:
            logger.error(f"[Indeed] Unexpected request error for '{keyword}': {exc}")
            return []

        jobs = self._parse(response.text, keyword)
        logger.info(f"[Indeed] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    # ------------------------------------------------------------------
    # Private parsing
    # ------------------------------------------------------------------
    def _parse(self, html: str, keyword: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        for card in soup.select(_SELECTORS["card"]):
            try:
                title_el = card.select_one(_SELECTORS["title"])
                company_el = card.select_one(_SELECTORS["company"])
                location_el = card.select_one(_SELECTORS["location"])

                # Try the dedicated job link first; fall back to first anchor
                link_el = card.select_one(_SELECTORS["link"]) or card.select_one("a")

                if not (title_el and company_el and link_el):
                    continue

                href = link_el.get("href", "")
                job_url = (
                    "https://in.indeed.com" + href
                    if href.startswith("/")
                    else href
                )

                jobs.append(
                    {
                        "Company": company_el.get_text(strip=True),
                        "Role": title_el.get_text(strip=True),
                        "Location": location_el.get_text(strip=True) if location_el else "",
                        "Platform": "Indeed",
                        "Keyword": keyword,
                        "Job Link": job_url,
                    }
                )
            except Exception as exc:
                logger.warning(f"[Indeed] Skipped a card due to parse error: {exc}")

        return jobs
