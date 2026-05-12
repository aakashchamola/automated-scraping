"""Normalization and validation helpers for employee, LinkedIn, and career fields."""

import re
from urllib.parse import urlparse


def is_na(value: str) -> bool:
    return (value or "").strip().upper() == "NA"


def is_valid_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ("http", "https") and parsed.netloc)
    except Exception:
        return False


def is_invalid_career_value(value: str, invalid_values: set) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    if v in invalid_values:
        return True
    if v == "://":
        return True
    return False


def is_valid_linkedin_url(value: str) -> bool:
    if not value:
        return False
    if "linkedin.com" not in value or not is_valid_url(value):
        return False
    return bool(re.search(r"linkedin\.com/(?:company|school)/[^/?#]+", value))


def normalize_linkedin_url(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    m = re.match(r"(https?://(?:www\.)?linkedin\.com/(?:company|school)/[^/?#]+)", v)
    if not m:
        return ""
    return m.group(1).rstrip("/") + "/"


def normalize_career_page(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if is_na(v) or v.strip().upper() in {"N/A", "NONE", "NULL", "-"}:
        return ""
    if is_valid_url(v):
        return v
    if "://" not in v and re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[\w\-./?%=&+#]*)?$", v):
        candidate = f"https://{v.lstrip('/')}"
        if is_valid_url(candidate):
            return candidate
    return ""


def normalize_employee_count(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    nums = re.findall(r"\d[\d,]*", raw)
    if not nums:
        return ""
    return nums[0].replace(",", "")


def is_numeric_employee_count(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    if is_na(v):
        return False
    return bool(
        re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\+)?", v, re.IGNORECASE)
        or re.fullmatch(r"\d+(?:\.\d+)?\s*[kmb](?:\+)?", v, re.IGNORECASE)
        or re.fullmatch(
            r"\d+(?:\.\d+)?(?:\s*[kmb])?\s*[-–]\s*\d+(?:\.\d+)?(?:\s*[kmb])?(?:\+)?",
            v,
            re.IGNORECASE,
        )
    )


def normalize_source_employee_count(value: str) -> str:
    raw = (value or "").strip()
    if not raw or is_na(raw) or raw.upper() in {"N/A", "NONE", "NULL", "-"}:
        return ""
    if is_numeric_employee_count(raw):
        return raw
    return normalize_employee_count(raw)
