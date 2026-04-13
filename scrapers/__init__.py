from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """
    Interface every platform scraper must implement.

    To add a new platform:
      1. Create scrapers/<platform>.py
      2. Subclass BaseScraper and implement fetch_jobs()
      3. Register the class in main.py SCRAPERS dict
    """

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def fetch_jobs(self, keyword: str) -> list:
        """
        Fetch job listings for *keyword* from this platform.

        Returns a list of dicts with at minimum:
            Company, Role, Location, Platform, Keyword, Job Link
        """
