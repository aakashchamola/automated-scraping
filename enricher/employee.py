"""Employee count scraping helpers for company enrichment."""

import json
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

_EMPLOYEE_PATTERNS = [
    r"see all\s*([\d,]+)\s*employees\s*on\s*linkedin",
    r"([\d,]+)\s*associated\s*members",
    r"([\d,]+\+?)\s*employees",
    r"employees[:\s]+([\d,]+\+?)",
    r"([\d,]+\+?)\s*staff",
    r"company size[:\s]+([^\n<]+)",
    r"([\d,]+\s*[-–]\s*[\d,]+)\s*employees",
]


def _scrape_website_for_employee_count(website: str) -> str:
    """Try to extract employee count from a company's About page."""
    if not website:
        return ""
    base = website.rstrip("/")
    pages_to_try = [f"{base}/about", f"{base}/about-us"]
    for page_url in pages_to_try:
        try:
            resp = requests.get(
                page_url,
                headers=_BROWSER_HEADERS,
                timeout=8,
                allow_redirects=True,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        # Check meta description tags first (fast path)
        for attr in ({"name": "description"}, {"property": "og:description"}):
            tag = soup.find("meta", attr)
            if tag and tag.get("content"):
                for pattern in _EMPLOYEE_PATTERNS:
                    m = re.search(pattern, tag["content"], re.IGNORECASE)
                    if m:
                        val = m.group(1).strip()
                        if len(val) <= 40:
                            return val

        text = soup.get_text(" ", strip=True)
        for pattern in _EMPLOYEE_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if len(val) <= 40:
                    return val
    return ""


def scrape_employee_count(linkedin_url: str, website: str = "") -> str:
    """Scrape employee count from LinkedIn company/school pages.

    Falls back to company website About page when LinkedIn yields nothing.
    """
    if not linkedin_url or "linkedin.com" not in linkedin_url:
        if website:
            return _scrape_website_for_employee_count(website)
        return ""

    def _canonical_company_base(url: str) -> str:
        match = re.search(
            r"(https?://(?:www\.)?linkedin\.com/(?:company|school)/[^/?#]+)",
            url or "",
        )
        if not match:
            return ""
        return match.group(1).rstrip("/") + "/"

    initial_base = _canonical_company_base(linkedin_url) or (linkedin_url.rstrip("/") + "/")
    queue = [initial_base + "people/", initial_base]
    seen = set()
    rate_limited_urls: list = []

    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        try:
            resp = requests.get(
                url,
                headers=_BROWSER_HEADERS,
                timeout=12,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            logger.warning(f"LinkedIn request failed for {url}: {exc}")
            continue

        redirected_base = _canonical_company_base(resp.url)
        if redirected_base and redirected_base != initial_base:
            for candidate in (redirected_base + "people/", redirected_base):
                if candidate not in seen and candidate not in queue:
                    queue.append(candidate)
            logger.debug(
                f"LinkedIn redirected company URL; queued canonical fallback: {redirected_base}"
            )

        if resp.status_code == 999:
            logger.warning(f"LinkedIn rate-limited (999) for {url} — will retry after delay")
            rate_limited_urls.append(url)
            continue
        if resp.status_code != 200:
            logger.warning(f"LinkedIn returned HTTP {resp.status_code} for {url}")
            continue
        if (
            "login" in resp.url
            or "authwall" in resp.url
            or ("/company/" not in resp.url and "/school/" not in resp.url)
        ):
            logger.warning(f"LinkedIn redirected to login/authwall for {url}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    employees = data.get("numberOfEmployees")
                    if isinstance(employees, dict):
                        value = employees.get("value")
                        if value:
                            return f"{value} employees"
                        min_value = employees.get("minValue")
                        max_value = employees.get("maxValue")
                        if min_value and max_value:
                            return f"{min_value}-{max_value} employees"
            except (json.JSONDecodeError, AttributeError):
                pass

        text = soup.get_text(" ", strip=True)
        for pattern in _EMPLOYEE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if len(value) > 40:
                    continue
                if value.replace(",", "").isdigit():
                    return f"{value} employees"
                return value

    # Retry URLs that were rate-limited (999) after a short sleep
    if rate_limited_urls:
        logger.info(f"Retrying {len(rate_limited_urls)} rate-limited LinkedIn URL(s) after 15s delay")
        time.sleep(15)
        for url in rate_limited_urls:
            try:
                resp = requests.get(
                    url,
                    headers=_BROWSER_HEADERS,
                    timeout=12,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                logger.warning(f"LinkedIn retry failed for {url}: {exc}")
                continue
            if resp.status_code != 200:
                continue
            if (
                "login" in resp.url
                or "authwall" in resp.url
                or ("/company/" not in resp.url and "/school/" not in resp.url)
            ):
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for script in soup.find_all("script", {"type": "application/ld+json"}):
                try:
                    data = json.loads(script.string or "")
                    if isinstance(data, dict):
                        employees = data.get("numberOfEmployees")
                        if isinstance(employees, dict):
                            value = employees.get("value")
                            if value:
                                return f"{value} employees"
                            min_value = employees.get("minValue")
                            max_value = employees.get("maxValue")
                            if min_value and max_value:
                                return f"{min_value}-{max_value} employees"
                except (json.JSONDecodeError, AttributeError):
                    pass
            text = soup.get_text(" ", strip=True)
            for pattern in _EMPLOYEE_PATTERNS:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    value = match.group(1).strip()
                    if len(value) > 40:
                        continue
                    if value.replace(",", "").isdigit():
                        return f"{value} employees"
                    return value

    # Website fallback: try to extract headcount from company About page
    if website:
        result = _scrape_website_for_employee_count(website)
        if result:
            logger.debug(f"employee_count (from website fallback): '{result}'")
            return result

    return ""
