"""Career page discovery helpers for company enrichment."""

import json
import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import normalizers
from . import search as ddg_search

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

_CAREER_PATHS = [
    "/careers",
    "/jobs",
    "/careers/",
    "/jobs/",
    "/work-with-us",
    "/join-us",
    "/join",
    "/about/careers",
    "/hiring",
    "/employment",
    "/work-here",
    "/opportunities",
    "/open-positions",
    "/openings",
    "/team/join",
    "/join-the-team",
    "/careers/jobs",
    "/jobs/all",
]

_CAREER_SUBDOMAINS = ["careers", "jobs", "work", "hire", "talent", "recruiting"]

# Known ATS / job-board domains — if a company's homepage links to one, that IS their career page
_JOB_BOARD_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "bamboohr.com",
    "smartrecruiters.com",
    "workable.com",
    "icims.com",
    "taleo.net",
    "jobvite.com",
    "recruitee.com",
    "breezy.hr",
    "pinpointhq.com",
    "rippling.com",
    "personio.com",
    "successfactors.com",
]

_CAREER_KEYWORDS = {"career", "careers", "job", "jobs", "hiring", "recruit", "join", "work-with"}

# Common TLD/suffix candidates when guessing a company domain from a slug.
_DOMAIN_TLDS = (".com", ".io", ".co", ".ai", ".net", ".org")


def _homepage_matches_slug(url: str, slug_tokens: list, timeout: int = 6) -> bool:
    """GET homepage; True iff its title/meta mentions any slug token.

    Guards slug-guessed domains against parked / squatted domains that
    happen to resolve but belong to nobody.
    """
    if not slug_tokens:
        return False
    try:
        resp = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    soup = BeautifulSoup(resp.text, "html.parser")
    haystack_parts = []
    if soup.title and soup.title.string:
        haystack_parts.append(soup.title.string)
    for meta_attr in ({"name": "description"}, {"property": "og:title"},
                       {"property": "og:description"}, {"property": "og:site_name"}):
        tag = soup.find("meta", meta_attr)
        if tag and tag.get("content"):
            haystack_parts.append(tag["content"])
    haystack = " ".join(haystack_parts).lower()
    return any(tok in haystack for tok in slug_tokens if len(tok) >= 3)


def _guess_website_from_linkedin_slug(linkedin_url: str) -> str:
    """Probe candidate domains derived from the LinkedIn slug.

    A candidate is accepted only if the homepage actually mentions the slug —
    guards against parked/squatted domains.
    """
    if not linkedin_url:
        return ""
    m = re.search(r"linkedin\.com/(?:company|school)/([^/?#]+)", linkedin_url)
    if not m:
        return ""
    slug = m.group(1).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", slug) if t]
    if not tokens:
        return ""

    roots = []
    for r in (slug, slug.replace("-", "")):
        if r and r not in roots:
            roots.append(r)

    for root in roots:
        for tld in _DOMAIN_TLDS:
            candidate = f"https://www.{root}{tld}"
            if not probe_url(candidate, timeout=5):
                # Try bare hostname too (some sites don't serve www.)
                bare = f"https://{root}{tld}"
                if not probe_url(bare, timeout=5):
                    continue
                candidate = bare
            if _homepage_matches_slug(candidate, tokens):
                logger.debug(f"website via slug guess (verified): {candidate}")
                return candidate
            logger.debug(f"slug-guess candidate {candidate} resolves but doesn't match slug — reject")
    return ""


def get_company_website(linkedin_url: str) -> str:
    """Extract company external website URL from LinkedIn org pages."""
    if not linkedin_url or "linkedin.com" not in linkedin_url:
        return ""

    normalized = normalizers.normalize_linkedin_url(linkedin_url)
    if not normalized:
        normalized = linkedin_url.rstrip("/") + "/"

    def _extract_external_site(soup: BeautifulSoup) -> str:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
                nodes = data.get("@graph", []) if isinstance(data, dict) else []
                if not nodes and isinstance(data, dict):
                    nodes = [data]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    same_as = node.get("sameAs", "")
                    if isinstance(same_as, list):
                        candidates = [
                            s
                            for s in same_as
                            if isinstance(s, str)
                            and "linkedin.com" not in s
                            and s.startswith("http")
                        ]

                        def _url_score(url: str) -> tuple:
                            parsed = urlparse(url)
                            subdomain_depth = parsed.netloc.count(".") - 1
                            path_depth = len([x for x in parsed.path.split("/") if x])
                            return (subdomain_depth, path_depth)

                        candidates.sort(key=_url_score)
                        same_as = candidates[0] if candidates else ""
                    if same_as and "linkedin.com" not in same_as:
                        return same_as

                    site = node.get("url", "")
                    if site and "linkedin.com" not in site:
                        return site
            except (json.JSONDecodeError, AttributeError):
                pass
        return ""

    candidate_urls = [normalized, normalized.rstrip("/") + "/about/"]
    seen_urls = set()
    for url in candidate_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)

        try:
            resp = requests.get(
                url,
                headers=_BROWSER_HEADERS,
                timeout=12,
                allow_redirects=True,
            )
        except requests.RequestException:
            continue

        if resp.status_code != 200:
            continue
        if (
            "login" in resp.url
            or "authwall" in resp.url
            or ("/company/" not in resp.url and "/school/" not in resp.url)
        ):
            continue

        website = _extract_external_site(BeautifulSoup(resp.text, "html.parser"))
        if website:
            return website

    return ""


def probe_url(url: str, timeout: int = 8) -> bool:
    """Return True if URL responds with a success status.

    Tries HEAD first; falls back to GET when the server refuses HEAD (405/403).
    """
    try:
        resp = requests.head(
            url,
            headers=_BROWSER_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code < 400:
            return True
        # Some servers block HEAD — retry with GET on 4xx
        if resp.status_code in (403, 405):
            resp2 = requests.get(
                url,
                headers=_BROWSER_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            return resp2.status_code < 400
        return False
    except requests.RequestException:
        return False


def _detect_job_board(website_url: str) -> str:
    """Fetch company homepage and look for links pointing to known ATS/job-board platforms."""
    if not website_url:
        return ""
    try:
        resp = requests.get(
            website_url,
            headers=_BROWSER_HEADERS,
            timeout=8,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href.startswith("http"):
                continue
            for board in _JOB_BOARD_DOMAINS:
                if board in href:
                    logger.debug(f"Career page via job board link on homepage: {href}")
                    return href
    except requests.RequestException:
        pass
    return ""


def _find_career_in_sitemap(base_url: str) -> str:
    """Parse sitemap.xml (and sitemap_index.xml) for career-related URLs."""
    if not base_url:
        return ""
    base = base_url.rstrip("/")
    for sitemap_path in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            resp = requests.get(
                base + sitemap_path,
                headers=_BROWSER_HEADERS,
                timeout=6,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", resp.text, re.IGNORECASE):
                loc_lower = loc.lower()
                if any(kw in loc_lower for kw in _CAREER_KEYWORDS):
                    logger.debug(f"Career page via sitemap: {loc}")
                    return loc
        except requests.RequestException:
            continue
    return ""


def find_career_page(company_name: str, linkedin_url: str, website: str = "") -> str:
    """Find company career page URL using LinkedIn-derived website and probes.

    Pass a pre-fetched ``website`` to skip the LinkedIn fetch when the caller
    already has it (avoids a duplicate HTTP round-trip).
    """
    if not website:
        website = get_company_website(linkedin_url)
    if not normalizers.is_valid_url(website):
        website = ""

    if not website and linkedin_url:
        # Slug-based domain guess: probe stripe.com / abbottlabs.com etc.
        # No network for search engines needed — pure HEAD probes.
        website = _guess_website_from_linkedin_slug(linkedin_url)
        if website:
            logger.info(
                f"Career page: website resolved via slug guess for '{company_name}': {website}"
            )

    if not website and company_name:
        # Final fallback: search engines (best-effort; may be IP-blocked).
        website = ddg_search.find_company_website(company_name)
        if website:
            logger.info(
                f"Career page: website resolved via search for '{company_name}': {website}"
            )

    if not website:
        logger.debug(
            f"No website (LinkedIn / slug / search) for '{company_name}', cannot determine career page"
        )
        return ""

    parsed = urlparse(website)
    base = f"{parsed.scheme}://{parsed.netloc}"
    bare_domain = parsed.netloc.lstrip("www.")

    # 1. Job board detection: if homepage links directly to Greenhouse/Lever/etc.
    job_board_url = _detect_job_board(website)
    if job_board_url:
        return job_board_url

    # 2. Subdomain probing
    for sub in _CAREER_SUBDOMAINS:
        candidate = f"{parsed.scheme}://{sub}.{bare_domain}"
        if probe_url(candidate):
            logger.debug(f"Career page via subdomain: {candidate}")
            return candidate

    # 3. Path probing
    for path in _CAREER_PATHS:
        candidate = base + path
        if probe_url(candidate):
            logger.debug(f"Career page via path: {candidate}")
            return candidate

    # 4. Sitemap.xml
    sitemap_career = _find_career_in_sitemap(base)
    if sitemap_career:
        return sitemap_career

    # 5. Base domain fallback — at minimum the company has a web presence
    logger.debug(f"Career page (base domain only) for '{company_name}': {base}")
    return base
