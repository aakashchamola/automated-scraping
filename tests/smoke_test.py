"""Quick platform smoke test.

Runs one keyword across configured platforms and prints result counts.
"""

from config_loader import load_config
from main import SCRAPERS, load_keywords, validate_config


def run_smoke(config_path: str = "config.yaml") -> None:
    config = validate_config(load_config(config_path))
    scraping = config.get("scraping", {})
    keywords = load_keywords(scraping.get("keywords_fallback_file", "keywords.txt"))
    if not keywords:
        print("No keywords found.")
        return

    keyword = keywords[0]
    print(f"Smoke keyword: {keyword}")

    for platform_name in scraping.get("platforms", []):
        scraper_cls = SCRAPERS.get(platform_name)
        if not scraper_cls:
            print(f"{platform_name:12} -> SKIPPED (no registered scraper)")
            continue

        try:
            scraper = scraper_cls(config)
            jobs = scraper.fetch_jobs(keyword)
            print(f"{platform_name:12} -> {len(jobs)} results")
        except Exception as exc:  # pragma: no cover
            print(f"{platform_name:12} -> ERROR: {exc}")


if __name__ == "__main__":
    run_smoke()
