import logging

from scrapers import BaseScraper

logger = logging.getLogger(__name__)


class YCombinatorScraper(BaseScraper):
    """Y Combinator / Work at a Startup scraper — currently non-functional.

    workatastartup.com is a fully client-side React SPA. All job listings
    are loaded via JS after the initial page render. The server returns an
    empty HTML shell — no job data is present for a standard HTTP request.

    A headless browser (Playwright/Selenium) would be required to scrape it.

    Note: YC-backed biotech companies are often already in the Company sheet
    and their career pages are scraped via career_page.py instead.
    """

    _BLOCKED_MSG = (
        "[YCombinator] workatastartup.com is JS-rendered (React SPA) — "
        "no job data available from plain HTTP requests. Skipping."
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def fetch_jobs(self, keyword: str) -> list:
        logger.warning(self._BLOCKED_MSG)
        return []
