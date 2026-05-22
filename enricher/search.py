"""Multi-engine search helper used as a scraping fallback.

Tries DuckDuckGo (POST html), Bing, and Mojeek in order; the first engine
that returns parseable results wins. Each engine call is throttled and the
module fails gracefully (returns []) when every engine is blocked.

Used by:
- employee.py: snippet-based employee count when LinkedIn pages are blocked
- career.py:   official website discovery when LinkedIn yields nothing
"""

import logging
import random
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_LAST_REQ_TS = 0.0
_MIN_GAP_SEC = 1.5

_NON_OFFICIAL_HOSTS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "wikipedia.org",
    "crunchbase.com", "bloomberg.com", "glassdoor.com",
    "indeed.com", "zoominfo.com", "rocketreach.co",
    "pitchbook.com", "owler.com", "yelp.com", "amazon.com",
    "reddit.com", "medium.com", "github.com", "duckduckgo.com",
    "bing.com", "mojeek.com", "google.com",
)


def _polite_sleep() -> None:
    global _LAST_REQ_TS
    now = time.time()
    gap = now - _LAST_REQ_TS
    if gap < _MIN_GAP_SEC:
        time.sleep(_MIN_GAP_SEC - gap)
    _LAST_REQ_TS = time.time()


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _resolve_ddg_redirect(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href


def _ddg(query: str, max_results: int) -> list:
    _polite_sleep()
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=_headers(),
            timeout=12,
        )
    except requests.RequestException as exc:
        logger.debug(f"DDG search error for {query!r}: {exc}")
        return []
    if resp.status_code != 200 or "anomaly" in resp.text[:2000]:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for r in soup.select(".result")[:max_results]:
        a = r.select_one("a.result__a")
        if not a:
            continue
        url = _resolve_ddg_redirect(a.get("href", ""))
        if not url:
            continue
        snip = r.select_one(".result__snippet")
        out.append({
            "url": url,
            "title": a.get_text(strip=True),
            "snippet": snip.get_text(" ", strip=True) if snip else "",
        })
    return out


def _bing(query: str, max_results: int) -> list:
    _polite_sleep()
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query},
            headers=_headers(),
            timeout=12,
        )
    except requests.RequestException as exc:
        logger.debug(f"Bing search error for {query!r}: {exc}")
        return []
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for li in soup.select("li.b_algo")[:max_results]:
        a = li.select_one("h2 a")
        if not a:
            continue
        url = a.get("href", "")
        if not url or not url.startswith("http"):
            continue
        snip = li.select_one(".b_caption p") or li.select_one("p")
        out.append({
            "url": url,
            "title": a.get_text(strip=True),
            "snippet": snip.get_text(" ", strip=True) if snip else "",
        })
    return out


def _mojeek(query: str, max_results: int) -> list:
    _polite_sleep()
    try:
        resp = requests.get(
            "https://www.mojeek.com/search",
            params={"q": query},
            headers=_headers(),
            timeout=12,
        )
    except requests.RequestException as exc:
        logger.debug(f"Mojeek search error for {query!r}: {exc}")
        return []
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one("ul.results-standard")
    if not container:
        return []
    out = []
    for li in container.find_all("li", recursive=False)[:max_results]:
        a = li.select_one("a.title") or li.select_one("h2 a")
        if not a:
            continue
        url = a.get("href", "")
        if not url or not url.startswith("http"):
            continue
        snip = li.find("p", class_="s")
        out.append({
            "url": url,
            "title": a.get_text(strip=True),
            "snippet": snip.get_text(" ", strip=True) if snip else "",
        })
    return out


def search(query: str, max_results: int = 8) -> list:
    """Return [{url, title, snippet}, ...] using whichever engine works.

    Tries DDG → Bing → Mojeek. Returns [] only when every engine is
    blocked/empty.
    """
    if not query:
        return []
    for fn, name in ((_ddg, "ddg"), (_bing, "bing"), (_mojeek, "mojeek")):
        results = fn(query, max_results)
        if results:
            logger.debug(f"search ok via {name} for {query!r}: {len(results)} hits")
            return results
        logger.debug(f"search engine {name} returned nothing for {query!r}")
    return []


def find_company_website(company_name: str) -> str:
    """Best-guess official site URL for ``company_name`` via search.

    Skips known social/news/directory hosts. Returns "" when no engine
    returns usable results.
    """
    if not company_name:
        return ""
    queries = [
        f'"{company_name}" official site',
        f'{company_name} official website',
    ]
    for q in queries:
        for r in search(q, max_results=10):
            url = r["url"]
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            if not host:
                continue
            if any(host == b or host.endswith("." + b) for b in _NON_OFFICIAL_HOSTS):
                continue
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


_EMPLOYEE_RANGE_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})*)\s*[-–]\s*(\d{1,3}(?:,\d{3})*)\s*employees",
    re.IGNORECASE,
)
_EMPLOYEE_COUNT_RE = re.compile(
    r"([\d,]+\+?)\s*(?:employees|associated members|staff)",
    re.IGNORECASE,
)
_SEE_ALL_RE = re.compile(
    r"see all\s*([\d,]+)\s*employees\s*on\s*linkedin",
    re.IGNORECASE,
)


def find_employee_count_via_search(company_name: str, linkedin_url: str = "") -> str:
    """Parse 'X employees' from search snippets — best-effort fallback.

    Returns "" when no engine yields a parseable snippet (common when the
    host IP is blocked).
    """
    if not company_name and not linkedin_url:
        return ""

    slug = ""
    org_path = ""
    if linkedin_url:
        m = re.search(
            r"linkedin\.com/(company|school)/([^/?#]+)",
            linkedin_url,
        )
        if m:
            org_path = m.group(1)
            slug = m.group(2)

    queries = []
    if slug and org_path:
        queries.append(f'site:linkedin.com/{org_path}/{slug} employees')
    if company_name:
        queries.append(f'site:linkedin.com/company "{company_name}" employees')
        queries.append(f'"{company_name}" linkedin employees')

    for qi, q in enumerate(queries):
        require_slug = (qi == 0)
        for r in search(q, max_results=8):
            url = r.get("url", "")
            if "linkedin.com" not in url:
                continue
            if require_slug and slug and slug.lower() not in url.lower():
                continue
            text = f"{r.get('title', '')} {r.get('snippet', '')}"
            m = _SEE_ALL_RE.search(text)
            if m and len(m.group(1)) <= 20:
                return f"{m.group(1).strip()} employees"
            m = _EMPLOYEE_RANGE_RE.search(text)
            if m:
                return f"{m.group(1)}-{m.group(2)} employees"
            m = _EMPLOYEE_COUNT_RE.search(text)
            if m and len(m.group(1)) <= 20:
                return f"{m.group(1).strip()} employees"
    return ""
