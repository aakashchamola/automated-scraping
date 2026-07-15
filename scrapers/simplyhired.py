import logging

from scrapers import BaseScraper

logger = logging.getLogger(__name__)


class SimplyHiredScraper(BaseScraper):
    """SimplyHired scraper — currently non-functional from server IPs.

    SimplyHired serves a Cloudflare "Just a moment..." challenge page (403)
    to all automated/non-browser requests from server IP ranges.
    """

    _BLOCKED_MSG = (
        "[SimplyHired] Blocked by Cloudflare anti-bot protection — "
        "skipping. Run from a residential/browser environment to use SimplyHired."
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def fetch_jobs(self, keyword: str) -> list:
        logger.warning(self._BLOCKED_MSG)
        return []
