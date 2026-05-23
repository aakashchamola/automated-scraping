import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_session(config: dict) -> requests.Session:
    req_cfg = config.get("http", {})
    retry = Retry(
        total=req_cfg.get("max_retries", 3),
        backoff_factor=req_cfg.get("retry_delay_seconds", 1),
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_html(
    session: requests.Session,
    url: str,
    config: dict,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> str:
    req_headers = headers or DEFAULT_HEADERS
    timeout = config.get("http", {}).get("timeout_seconds", 10)

    response = session.get(url, params=params, headers=req_headers, timeout=timeout)
    response.raise_for_status()
    return response.text
