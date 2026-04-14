import logging
import time
from urllib.parse import urljoin

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
    "link": "h2.jobTitle a, a[id^='job_']",
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
    """Scraper for Indeed with configurable country/location."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._base_domain = self._resolve_domain()
        self._base_url = f"https://{self._base_domain}/jobs"
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
        jobs = []
        delay = float(self.config.get("request", {}).get("delay_between_requests", 0))
        location_query = self._location_query()
        country_code = self._country_code()

        for start in self._page_starts():
            params = {
                "q": keyword,
                "l": location_query,
                "start": start,
            }
            logger.info(
                f"[Indeed] GET {self._base_url} "
                f"| country='{country_code}' | keyword='{keyword}' "
                f"| location='{location_query}' | start={start}"
            )

            try:
                response = self._session.get(
                    self._base_url,
                    params=params,
                    headers=_HEADERS,
                    timeout=self.config.get("request", {}).get("timeout", 10),
                )
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                logger.error(f"[Indeed] HTTP error for '{keyword}': {exc}")
                continue
            except requests.exceptions.ConnectionError as exc:
                logger.error(f"[Indeed] Connection error for '{keyword}': {exc}")
                continue
            except requests.exceptions.Timeout:
                logger.error(f"[Indeed] Request timed out for '{keyword}'")
                continue
            except requests.exceptions.RequestException as exc:
                logger.error(f"[Indeed] Unexpected request error for '{keyword}': {exc}")
                continue

            page_jobs = self._parse(response.text, keyword)
            jobs.extend(page_jobs)

            if delay > 0:
                time.sleep(delay)

        logger.info(f"[Indeed] Found {len(jobs)} jobs for '{keyword}'")
        return jobs

    def _location_query(self) -> str:
        indeed_cfg = self.config.get("platform_settings", {}).get("indeed", {})
        return str(indeed_cfg.get("location", "United States")).strip()

    def _country_code(self) -> str:
        indeed_cfg = self.config.get("platform_settings", {}).get("indeed", {})
        return str(indeed_cfg.get("country", "us")).strip().lower()

    def _resolve_domain(self) -> str:
        domains = {
            "us": "www.indeed.com",
            "in": "in.indeed.com",
            "uk": "uk.indeed.com",
            "ca": "ca.indeed.com",
            "au": "au.indeed.com",
        }
        return domains.get(self._country_code(), "www.indeed.com")

    def _page_starts(self) -> list:
        indeed_cfg = self.config.get("platform_settings", {}).get("indeed", {})
        max_pages = int(indeed_cfg.get("max_pages", 1))
        max_pages = max(1, max_pages)
        return [page * 10 for page in range(max_pages)]

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

                if not link_el:
                    logger.warning("[Indeed] Skipping card: missing link")
                    continue

                href = link_el.get("href", "")
                job_url = urljoin(f"https://{self._base_domain}", href) if href else ""

                role = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else ""
                location = location_el.get_text(strip=True) if location_el else ""

                if not (company or role or job_url):
                    logger.warning("[Indeed] Skipping empty card")
                    continue

                jobs.append(
                    {
                        "Company": company,
                        "Role": role,
                        "Location": location,
                        "Platform": "Indeed",
                        "Keyword": keyword,
                        "Job Link": job_url,
                    }
                )
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning(f"[Indeed] Skipped a card due to parse error: {exc}")

        return jobs
