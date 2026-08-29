"""
Career page scraper — scrape real job postings from company career pages.

Strategy (in priority order):
  1. Detect the company's ATS (Greenhouse / Lever / Ashby) from the career
     page and pull clean, structured jobs from that ATS's public JSON API.
     This avoids the "junk links" problem of JS-rendered career pages.
  2. Fall back to HTML anchor extraction, restricted to URLs that look like a
     specific job posting (carry a listing id), so we don't emit nav/CTA links.

Output dicts use the canonical job schema (storage.OUTPUT_COLUMNS):
    Company, Role, Location, Platform, Keyword, Job Link
"""

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from enricher import normalizers

logger = logging.getLogger(__name__)

# Short/connector tokens ignored when matching a keyword against a job title.
_KEYWORD_STOPWORDS = {
    "and", "or", "of", "the", "for", "in", "at", "to", "a", "an", "with",
    "&", "-", "/",
}


def _keyword_tokens(keyword: str) -> list:
    """Significant lowercase tokens of a keyword (drops stopwords / short bits)."""
    raw = re.split(r"[^a-z0-9+]+", (keyword or "").lower())
    return [t for t in raw if len(t) >= 3 and t not in _KEYWORD_STOPWORDS]


def match_keyword(title: str, keywords: list) -> str:
    """Return the first keyword whose significant tokens all appear in ``title``.

    Word-boundary, case-insensitive. Returns "" when nothing matches.
    """
    title_l = (title or "").lower()
    for kw in keywords:
        tokens = _keyword_tokens(kw)
        if not tokens:
            continue
        if all(re.search(rf"\b{re.escape(tok)}", title_l) for tok in tokens):
            return kw
    return ""


def normalize_career_url(value: str) -> str:
    """Normalize a Career-Page cell into a fetchable URL, or '' if unusable.

    Handles bare domains ('careers.acme.com'), domain+path
    ('acme.com/careers'), and rejects 'N/A (no public career page)' etc.
    """
    return normalizers.normalize_career_page(value)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── ATS detection ───────────────────────────────────────────────────────────

_ATS_DETECTORS = (
    # (ats_name, regex capturing the board token/slug)
    ("greenhouse", re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board/js\?for=|v1/boards/)?([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"grnhse[^a-z0-9]+for['\"\s:=]+([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([a-z0-9_-]+)", re.I)),
)


# Workday needs three parts, not one slug: the tenant, the data centre it is
# hosted in (wd1, wd5, wd103 …) and the career site name. The optional
# "en-US"-style locale segment sits between the host and the site and must not
# be mistaken for it.
_WORKDAY = re.compile(
    r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)

_WORKDAY_NOT_A_SITE = {"login", "userhome", "job", "wday", "en-us"}


def detect_ats(html: str, final_url: str = "") -> tuple:
    """Return (ats_name, token) detected from page HTML / URL, or (None, None)."""
    haystack = f"{final_url}\n{html or ''}"

    for match in _WORKDAY.finditer(haystack):
        tenant, datacenter, site = match.group(1), match.group(2).lower(), match.group(3)
        if site.lower() in _WORKDAY_NOT_A_SITE:
            continue
        return "workday", f"{tenant}/{datacenter}/{site}"

    for ats_name, pattern in _ATS_DETECTORS:
        m = pattern.search(haystack)
        if m:
            token = m.group(1)
            # Guard against matching generic words captured as a token.
            if token and token.lower() not in {"embed", "js", "v1", "boards"}:
                return ats_name, token
    return None, None


# ── ATS JSON parsers (pure functions, unit-tested) ──────────────────────────

def parse_greenhouse(payload: dict, company: str, keyword: str) -> list:
    jobs = []
    for j in (payload or {}).get("jobs", []):
        title = (j.get("title") or "").strip()
        url = (j.get("absolute_url") or "").strip()
        loc = ((j.get("location") or {}).get("name") or "").strip()
        if title and url:
            jobs.append(_job(company, title, loc, url, keyword))
    return jobs


def parse_lever(payload: list, company: str, keyword: str) -> list:
    jobs = []
    for j in payload or []:
        title = (j.get("text") or "").strip()
        url = (j.get("hostedUrl") or "").strip()
        loc = ((j.get("categories") or {}).get("location") or "").strip()
        if title and url:
            jobs.append(_job(company, title, loc, url, keyword))
    return jobs


def parse_ashby(payload: dict, company: str, keyword: str) -> list:
    jobs = []
    for j in (payload or {}).get("jobs", []):
        title = (j.get("title") or "").strip()
        url = (j.get("jobUrl") or j.get("applyUrl") or "").strip()
        loc = (j.get("location") or "").strip()
        if title and url:
            jobs.append(_job(company, title, loc, url, keyword))
    return jobs


def parse_workday(payload: dict, company: str, keyword: str, base: str = "") -> list:
    """Workday returns paths, not URLs — externalPath must be joined to the site."""
    jobs = []
    for j in (payload or {}).get("jobPostings", []):
        title = (j.get("title") or "").strip()
        path = (j.get("externalPath") or "").strip()
        loc = (j.get("locationsText") or "").strip()
        if title and path:
            jobs.append(_job(company, title, loc, f"{base}{path}", keyword))
    return jobs


def fetch_workday_jobs(
    token: str, company: str, keyword: str, timeout: int = 15, max_jobs: int = 200
) -> list:
    """Page through a Workday career site's public JSON API.

    Workday is the ATS behind most large employers in the company list (Pfizer,
    BMS, J&J, Amgen, Gilead, Abbott …). Its career pages are JavaScript shells
    with no jobs in the HTML, so without this they scrape to zero. The endpoint
    the page's own front-end calls is public and takes a POST.
    """
    try:
        tenant, datacenter, site = token.split("/")
    except ValueError:
        return []

    host = f"https://{tenant}.{datacenter}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    base = f"{host}/en-US/{site}"
    headers = dict(_BROWSER_HEADERS)
    headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    jobs, offset, page_size = [], 0, 20
    while len(jobs) < max_jobs:
        try:
            resp = requests.post(
                api, json={"appliedFacets": {}, "limit": page_size,
                           "offset": offset, "searchText": ""},
                headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning(f"[career] workday fetch failed for {company!r}: {exc}")
            break
        if resp.status_code != 200:
            logger.debug(f"[career] workday HTTP {resp.status_code} for {token!r}")
            break
        try:
            payload = resp.json()
        except ValueError:
            break

        page = parse_workday(payload, company, keyword, base=base)
        if not page:
            break
        jobs.extend(page)
        offset += page_size
        if offset >= int(payload.get("total") or 0):
            break
    return jobs[:max_jobs]


_ATS_API = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        parse_greenhouse,
    ),
    "lever": (
        "https://api.lever.co/v0/postings/{token}?mode=json",
        parse_lever,
    ),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{token}",
        parse_ashby,
    ),
}


def fetch_ats_jobs(
    ats: str, token: str, company: str, keyword: str, timeout: int = 15
) -> list:
    """Fetch + parse jobs from a detected ATS JSON API."""
    if ats == "workday":
        return fetch_workday_jobs(token, company, keyword, timeout=timeout)

    spec = _ATS_API.get(ats)
    if not spec:
        return []
    url_tmpl, parser = spec
    url = url_tmpl.format(token=token)
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning(f"[career] ATS {ats} fetch failed for {company!r}: {exc}")
        return []
    if resp.status_code != 200:
        logger.debug(f"[career] ATS {ats} HTTP {resp.status_code} for token {token!r}")
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    return parser(payload, company, keyword)


# ── HTML fallback (tightened: require a listing id) ─────────────────────────

# A posting URL almost always carries a unique listing identifier. Requiring
# one filters out nav / CTA links like /jobs, /benefits, /life-at-company.
_LISTING_PATTERNS = (
    re.compile(r"gh_jid=\d+"),
    re.compile(r"/o/[0-9a-f-]{20,}", re.I),       # lever posting uuid
    re.compile(r"/jobs?/\d+"),                      # /job/123 /jobs/123
    re.compile(r"/jobs?/[^/?#]*-\d{3,}"),           # slug-1234567
    re.compile(r"/listing/"),
    re.compile(r"/positions?/\d+", re.I),
    re.compile(r"job[_-]?id=\w+", re.I),
    re.compile(r"/requisitions?/", re.I),
)

_JUNK_TITLES = {
    "apply", "apply now", "view", "view job", "view all", "view all jobs",
    "learn more", "read more", "see more", "see all jobs", "see open roles",
    "details", "open positions", "all jobs", "careers", "career", "jobs",
    "search jobs", "join us", "join our team", "explore", "browse jobs",
    "home", "about", "about us", "contact", "contact us", "login", "sign in",
    "privacy policy", "terms", "cookie policy", "back", "next", "previous",
    "life at stripe", "benefits", "university", "edit this page",
    "get your free trial",
}


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_listing_href(href: str) -> bool:
    return any(p.search(href) for p in _LISTING_PATTERNS)


def _is_real_title(title: str) -> bool:
    if not title or len(title) < 3:
        return False
    if title.lower() in _JUNK_TITLES:
        return False
    return bool(re.search(r"[A-Za-z]", title))


def extract_jobs_from_html(html: str, base_url: str, max_jobs: int = 25) -> list:
    """Return [{title, url, location}] for posting-like links in ``html``.

    Only links whose URL carries a listing id are kept, so generic nav/CTA
    links are excluded.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if not _is_listing_href(href):
            continue
        full = urljoin(base_url, href).split("#", 1)[0]
        if full in seen:
            continue
        title = _clean_title(a.get_text(" ", strip=True))
        if not _is_real_title(title):
            continue
        seen.add(full)
        out.append({"title": title, "url": full, "location": ""})
        if len(out) >= max_jobs:
            break
    return out


# ── Public API ──────────────────────────────────────────────────────────────

def _job(company: str, role: str, location: str, link: str, keyword: str) -> dict:
    return {
        "Company": company,
        "Role": role,
        "Location": location,
        "Platform": "career_page",
        "Keyword": keyword,
        "Job Link": link,
    }


_CAREER_SUBDOMAINS = {"careers", "career", "jobs", "job", "work", "apply", "talent", "recruiting"}


def _url_candidates(url: str) -> list:
    """Yield the URL plus sensible variants to survive rough Company-sheet values.

    e.g. 'https://careers.veranahealth.com' also tries
    'https://www.veranahealth.com/careers' and 'https://veranahealth.com/careers'.
    Toggles the www. prefix too.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    path = parsed.path or ""
    out = [url]

    def _add(h, p):
        candidate = f"{scheme}://{h}{p}"
        if candidate not in out:
            out.append(candidate)

    # Toggle www on the original host.
    if host.startswith("www."):
        _add(host[4:], path)
    else:
        _add("www." + host, path)

    # Career-style subdomain -> apex + /careers (and www variant).
    labels = host.split(".")
    if len(labels) >= 3 and labels[0].lower() in _CAREER_SUBDOMAINS:
        apex = ".".join(labels[1:])
        _add(apex, "/careers")
        _add("www." + apex, "/careers")
    return out


def _fetch_career_html(url: str, timeout: int):
    """GET the career page, trying URL variants. Returns response or None."""
    candidates = _url_candidates(url)
    for candidate in candidates:
        try:
            resp = requests.get(
                candidate, headers=_BROWSER_HEADERS, timeout=timeout, allow_redirects=True
            )
        except requests.RequestException as exc:
            logger.debug(f"[career] fetch failed {candidate}: {exc}")
            continue
        if resp.status_code == 200:
            if resp.url != candidate:
                logger.debug(f"[career] redirected → {resp.url}")
            return resp
        logger.debug(f"[career] HTTP {resp.status_code}: {candidate}")
    logger.warning(f"[career] all URL candidates failed for: {url}")
    return None


def _filter_by_keywords(raw_jobs: list, keywords: list) -> list:
    """Keep only jobs whose Role matches a keyword; tag Keyword with the match.

    When ``keywords`` is empty, all jobs are kept (Keyword left as-is).
    """
    if not keywords:
        return raw_jobs
    kept = []
    for job in raw_jobs:
        matched = match_keyword(job.get("Role", ""), keywords)
        if matched:
            job["Keyword"] = matched
            kept.append(job)
    return kept


def scrape_career_page(
    company_name: str,
    career_page_url: str,
    keywords: list = None,
    max_jobs: int = 50,
    timeout: int = 15,
) -> list:
    """Scrape real job postings from a company's career page.

    Tries the company's ATS JSON API first, then HTML fallback. When
    ``keywords`` is provided, only jobs whose title matches a keyword are
    returned (and tagged with the matched keyword) — mirroring how the
    keyword platform scrapers return keyword-relevant jobs.
    """
    url = normalize_career_url(career_page_url)
    if not url:
        logger.debug(f"[career] {company_name!r}: unusable career page {career_page_url!r}")
        return []

    resp = _fetch_career_html(url, timeout)
    if resp is None:
        logger.warning(f"[career] could not fetch any career URL for {company_name!r} ({url})")
        return []

    # 1. ATS JSON API (clean, structured) — fetch ALL, filter by keyword after.
    raw = []
    ats, token = detect_ats(resp.text, resp.url)
    if ats and token:
        logger.info(f"[career] {company_name!r}: detected {ats} board '{token}'")
        raw = fetch_ats_jobs(ats, token, company_name, "career_page")
        if not raw:
            logger.info(f"[career] {company_name!r}: {ats} API empty, trying HTML")

    # 2. HTML fallback (listing-id links only)
    if not raw:
        found = extract_jobs_from_html(resp.text, resp.url, max_jobs=200)
        raw = [
            _job(company_name, item["title"], item["location"], item["url"], "career_page")
            for item in found
        ]

    if not raw:
        logger.info(f"[career] {company_name!r}: no postings found ({url})")
        return []

    jobs = _filter_by_keywords(raw, keywords or [])[:max_jobs]
    logger.info(
        f"[career] {company_name!r}: {len(jobs)} keyword-matched jobs "
        f"(of {len(raw)} scraped)"
    )
    return jobs


def scrape_companies(
    companies: list,
    keywords: list = None,
    company_col: str = "Company",
    career_col: str = "Career-Page",
    delay_sec: float = 2.0,
    max_jobs: int = 50,
) -> list:
    """Scrape keyword-matched jobs from a list of company row-dicts.

    Returns a flat job list. ``keywords`` filters jobs to relevant roles.
    """
    # Pre-filter to companies that have a usable career page URL
    workable = [
        row for row in companies
        if (row.get(company_col) or "").strip()
        and normalize_career_url((row.get(career_col) or "").strip())
    ]
    skipped = len(companies) - len(workable)
    logger.info(
        f"[career] {len(companies)} companies total | "
        f"{len(workable)} with usable career pages | {skipped} skipped (N/A or blank)"
    )

    all_jobs = []
    for i, row in enumerate(workable, start=1):
        company = row.get(company_col, "").strip()
        career_raw = row.get(career_col, "").strip()
        logger.info(f"[career] [{i}/{len(workable)}] {company!r}  →  {career_raw}")
        jobs = scrape_career_page(company, career_raw, keywords=keywords, max_jobs=max_jobs)
        all_jobs.extend(jobs)
        if delay_sec > 0 and i < len(workable):
            time.sleep(delay_sec)

    logger.info(f"[career] done — {len(all_jobs)} keyword-matched postings across {len(workable)} companies")
    return all_jobs
