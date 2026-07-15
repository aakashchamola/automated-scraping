import logging

from scrapers import BaseScraper

logger = logging.getLogger(__name__)


class WellfoundScraper(BaseScraper):
    """Wellfound (AngelList) scraper — currently non-functional from server IPs.

    Wellfound returns 403 Forbidden for all search requests made from
    non-browser or data-centre IP addresses (Cloudflare protection).
    """

    _BLOCKED_MSG = (
        "[Wellfound] Blocked by Cloudflare anti-bot protection — "
        "skipping. Run from a residential/browser environment to use Wellfound."
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def fetch_jobs(self, keyword: str) -> list:
        logger.warning(self._BLOCKED_MSG)
        return []
