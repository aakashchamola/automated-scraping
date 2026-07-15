import logging

from scrapers import BaseScraper

logger = logging.getLogger(__name__)


class GlassdoorScraper(BaseScraper):
    """Glassdoor scraper — currently non-functional from server IPs.

    Glassdoor's search pages are protected by Cloudflare and return an
    anti-bot challenge (403 / security page) when accessed from non-browser
    or data-centre IP addresses. The scraper detects this on the first
    request and skips silently for all subsequent keywords.

    To re-enable: remove 'glassdoor' from scraping.platforms in config.yaml
    only when running from a residential IP with a real browser session.
    """

    _BLOCKED_MSG = (
        "[Glassdoor] Blocked by Cloudflare anti-bot protection — "
        "skipping all keywords. Run from a residential/browser environment to use Glassdoor."
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._blocked = True   # treat as permanently blocked from server IPs

    def fetch_jobs(self, keyword: str) -> list:
        logger.warning(self._BLOCKED_MSG)
        return []
