"""Employee count scraping helpers for company enrichment."""

import json
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from . import search as ddg_search

logger = logging.getLogger(__name__)

# Pool of User-Agents we rotate through when LinkedIn rate-limits (999).
# Different UAs sometimes get served the cached public page instead of authwall.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# Backoff schedule applied between rotation passes when LinkedIn keeps 999'ing.
_RETRY_BACKOFF_SEC = [15, 45, 90]


def _headers_for(attempt: int) -> dict:
    return {
        "User-Agent": _USER_AGENTS[attempt % len(_USER_AGENTS)],
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
                headers=_headers_for(0),
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


def _extract_employee_from_soup(soup: BeautifulSoup) -> str:
    """Pull employee count from JSON-LD or visible page text. '' if not found."""
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(data, dict):
            continue
        employees = data.get("numberOfEmployees")
        if isinstance(employees, dict):
            value = employees.get("value")
            if value:
                return f"{value} employees"
            min_value = employees.get("minValue")
            max_value = employees.get("maxValue")
            if min_value and max_value:
                return f"{min_value}-{max_value} employees"

    text = soup.get_text(" ", strip=True)
    for pattern in _EMPLOYEE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) > 40:
            continue
        if value.replace(",", "").isdigit():
            return f"{value} employees"
        return value
    return ""


def _canonical_company_base(url: str) -> str:
    match = re.search(
        r"(https?://(?:www\.)?linkedin\.com/(?:company|school)/[^/?#]+)",
        url or "",
    )
    if not match:
        return ""
    return match.group(1).rstrip("/") + "/"


def _fetch_with_rotation(url: str):
    """GET ``url`` retrying with UA rotation + backoff only on 999.

    Returns (soup, final_url) on success, (None, "") otherwise.
    Authwall redirects don't retry — they always require login, so retrying
    is wasted time.
    """
    for attempt, backoff in enumerate([0] + _RETRY_BACKOFF_SEC):
        if backoff:
            logger.info(
                f"LinkedIn 999 retry {attempt}/{len(_RETRY_BACKOFF_SEC)} for {url} "
                f"after {backoff}s (rotating UA)"
            )
            time.sleep(backoff)
        try:
            resp = requests.get(
                url,
                headers=_headers_for(attempt),
                timeout=12,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            logger.warning(f"LinkedIn request failed for {url}: {exc}")
            continue

        if resp.status_code == 999:
            logger.warning(
                f"LinkedIn rate-limited (999) for {url} (attempt {attempt + 1})"
            )
            continue
        if resp.status_code != 200:
            logger.warning(f"LinkedIn returned HTTP {resp.status_code} for {url}")
            return None, ""
        if (
            "login" in resp.url
            or "authwall" in resp.url
            or ("/company/" not in resp.url and "/school/" not in resp.url)
        ):
            logger.warning(f"LinkedIn redirected to login/authwall for {url}")
            return None, resp.url

        return BeautifulSoup(resp.text, "html.parser"), resp.url

    return None, ""


def scrape_employee_count(
    linkedin_url: str,
    website: str = "",
    company_name: str = "",
) -> str:
    """Scrape employee count from LinkedIn company/school pages.

    Order of attempts:
      1. LinkedIn /people/ and main page (with UA rotation + backoff on 999)
      2. Search-engine snippet ('X employees' parsed from DDG results)
      3. Company website About page
    """
    snippet_company = company_name

    if linkedin_url and "linkedin.com" in linkedin_url:
        initial_base = (
            _canonical_company_base(linkedin_url)
            or (linkedin_url.rstrip("/") + "/")
        )
        # Derive a name hint from the slug if caller didn't provide one.
        if not snippet_company:
            m = re.search(r"/(?:company|school)/([^/?#]+)", initial_base)
            if m:
                snippet_company = m.group(1).replace("-", " ")

        queue = [initial_base + "people/", initial_base]
        seen = set()

        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            soup, final_url = _fetch_with_rotation(url)
            if soup is None:
                continue

            redirected_base = _canonical_company_base(final_url)
            if redirected_base and redirected_base != initial_base:
                for candidate in (redirected_base + "people/", redirected_base):
                    if candidate not in seen and candidate not in queue:
                        queue.append(candidate)
                logger.debug(
                    f"LinkedIn redirected to canonical {redirected_base}; queued"
                )

            value = _extract_employee_from_soup(soup)
            if value:
                return value

    # Search-engine snippet fallback (LinkedIn fully blocked or no value parsed)
    if linkedin_url or snippet_company:
        snippet_value = ddg_search.find_employee_count_via_search(
            snippet_company, linkedin_url
        )
        if snippet_value:
            logger.info(
                f"employee_count via DDG snippet for "
                f"{linkedin_url or snippet_company!r}: '{snippet_value}'"
            )
            return snippet_value

    # Website fallback: try to extract headcount from company About page
    if website:
        result = _scrape_website_for_employee_count(website)
        if result:
            logger.debug(f"employee_count (from website fallback): '{result}'")
            return result

    return ""
