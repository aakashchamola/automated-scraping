"""LinkedIn URL discovery and validation helpers."""

import logging
import re
import time

import requests

from . import normalizers

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


def linkedin_profile_status(url: str) -> str:
    """Return one of: valid, invalid, unknown for LinkedIn org profile URL."""
    normalized = normalizers.normalize_linkedin_url(url)
    if not normalized:
        return "invalid"

    try:
        resp = requests.get(
            normalized,
            headers=_BROWSER_HEADERS,
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException:
        return "unknown"

    if resp.status_code == 999:
        return "unknown"

    final = resp.url or ""
    if resp.status_code == 200:
        if "/search/" in final:
            return "invalid"
        if re.search(r"linkedin\.com/(?:company|school)/[^/?#]+", final):
            return "valid"
        if "authwall" in final:
            from urllib.parse import parse_qs, unquote, urlparse as _up

            qs = parse_qs(_up(final).query)
            ref = unquote(qs.get("referrerUrl", [""])[0])
            if re.search(r"linkedin\.com/(?:company|school)/[^/?#]+", ref):
                return "valid"
            return "invalid"

    if resp.status_code == 404:
        return "invalid"

    return "unknown"


def linkedin_slug_candidates(company_name: str) -> list:
    """Generate potential LinkedIn company URL slugs from a plain company name."""
    import unicodedata

    name = unicodedata.normalize("NFKD", company_name)
    name = name.encode("ascii", "ignore").decode("ascii")

    def to_slug(value: str) -> str:
        value = re.sub(r"\s*&\s*", " and ", value)
        value = value.lower()
        value = re.sub(r"[^a-z0-9\s-]", "", value)
        value = re.sub(r"\s+", "-", value.strip())
        value = re.sub(r"-+", "-", value).strip("-")
        return value

    seen = []

    def _add(s: str) -> None:
        slug = to_slug(s)
        if slug and slug not in seen:
            seen.append(slug)

    _add(name)

    # Strip legal suffixes
    name_stripped = re.sub(
        r"\s*\b(llc|inc\.?|corp\.?|ltd\.?|co\.?|plc|gmbh|ag|sa|bv|nv|lp|llp|pvt\.?|limited|incorporated|corporation)\b",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    _add(name_stripped)

    # Strip leading "the " (e.g. "The New York Times" → "new-york-times")
    name_no_the = re.sub(r"^the\s+", "", name_stripped, flags=re.IGNORECASE).strip()
    _add(name_no_the)

    # Strip parenthetical qualifiers (e.g. "Pfizer (US)" → "pfizer", "Company (2022)" → "company")
    name_no_paren = re.sub(r"\s*\(.*?\)", "", name_stripped).strip()
    _add(name_no_paren)
    name_no_paren_no_the = re.sub(r"^the\s+", "", name_no_paren, flags=re.IGNORECASE).strip()
    _add(name_no_paren_no_the)

    # Acronym slug for multi-word names (e.g. "Massachusetts Institute of Technology" → "mit")
    _STOP_WORDS = {"of", "in", "at", "the", "and", "or", "for", "a", "an", "by", "to", "its"}
    words = [
        w for w in re.split(r"[\s\-]+", name_no_paren_no_the)
        if len(w) >= 2 and w.lower() not in _STOP_WORDS
    ]
    if len(words) >= 3:
        acronym = "".join(w[0].lower() for w in words if w[0].isalpha())
        if len(acronym) >= 2:
            _add(acronym)

    return seen


def find_linkedin_url(company_name: str) -> str:
    """Discover an organization's LinkedIn URL by probing likely slugs directly."""
    slugs = linkedin_slug_candidates(company_name)
    for slug in slugs:
        for org_path in ("company", "school"):
            url = f"https://www.linkedin.com/{org_path}/{slug}/"
            try:
                resp = requests.get(
                    url,
                    headers=_BROWSER_HEADERS,
                    timeout=10,
                    allow_redirects=True,
                )
                if resp.status_code == 999:
                    logger.debug(f"LinkedIn slug probe 999 (inconclusive): {url}")
                    continue
                if resp.status_code == 200:
                    final = resp.url
                    if f"/{org_path}/" in final and "/search/" not in final and "authwall" not in final:
                        return url
                    if "authwall" in final:
                        from urllib.parse import parse_qs, unquote, urlparse as _up

                        qs = parse_qs(_up(final).query)
                        ref = unquote(qs.get("referrerUrl", [""])[0])
                        if f"/{org_path}/{slug}" in ref:
                            return url
            except requests.RequestException as exc:
                logger.debug(f"LinkedIn probe failed for {url}: {exc}")
            time.sleep(1)
    return ""
