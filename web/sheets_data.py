"""
web/sheets_data.py — Read-only Google Sheets access for the dashboard.

The Sheets API is rate-limited and every read costs a round trip, so results
are cached per worksheet with a short TTL and a manual refresh. Without this a
page with three tables would burn a quota unit on every keystroke in the filter
box.

Rows come back as plain dicts keyed by the sheet's own header, plus a
``_row`` field carrying the real 1-indexed sheet row — that is what lets the
dashboard link a table row straight to the cell it came from.
"""

import re
import threading
import time

from config_loader import load_config
from google_sheets_store import GoogleSheetsStore

_CACHE_TTL_SECONDS = 60
_cache = {}
_lock = threading.Lock()

# "=HYPERLINK("https://...","Acme Inc")" — the sheet stores a formula, but a
# table wants the label and the link separately.
_HYPERLINK = re.compile(r'^=HYPERLINK\(\s*"(?P<url>[^"]*)"\s*,\s*"(?P<label>.*)"\s*\)$', re.IGNORECASE)


def _config():
    return load_config("config.yaml")


def _unwrap(value: str):
    """Split a HYPERLINK formula into (display text, url)."""
    match = _HYPERLINK.match((value or "").strip())
    if match:
        return match.group("label").replace('""', '"'), match.group("url")
    return value, None


def read(worksheet: str, force: bool = False) -> dict:
    """Header + row dicts for one worksheet, cached for _CACHE_TTL_SECONDS."""
    now = time.time()
    with _lock:
        hit = _cache.get(worksheet)
        if hit and not force and now - hit["fetched_at"] < _CACHE_TTL_SECONDS:
            return hit

    cfg = _config()
    store = GoogleSheetsStore(cfg["google_sheets"])
    raw = store.load_all_rows(worksheet)

    header = [h.strip() for h in raw[0]] if raw else []
    # Sheets pads tabs out to their column count; drop the trailing blanks so
    # the table does not render a run of nameless columns.
    while header and not header[-1]:
        header.pop()

    rows = []
    for offset, values in enumerate(raw[1:], start=2):
        record = {"_row": offset}
        blank = True
        for index, name in enumerate(header):
            if not name:
                continue
            cell = values[index] if index < len(values) else ""
            text, url = _unwrap(cell)
            record[name] = text
            if url:
                record[f"{name}__url"] = url
            if text.strip():
                blank = False
        if not blank:
            rows.append(record)

    payload = {
        "worksheet": worksheet,
        "columns": [h for h in header if h],
        "rows": rows,
        "row_count": len(rows),
        "fetched_at": time.time(),
    }
    with _lock:
        _cache[worksheet] = payload
    return payload


def invalidate(worksheet: str = None) -> None:
    """Drop cached data — called after a run so the tables show its results."""
    with _lock:
        if worksheet:
            _cache.pop(worksheet, None)
        else:
            _cache.clear()


def targets() -> dict:
    """The worksheet names the dashboard should offer, straight from config so
    switching Settings to the production tabs also switches the tables."""
    cfg = _config()
    sheets = cfg["google_sheets"]
    return {
        "jobs": sheets.get("jobs_worksheet", "Jobs_Test"),
        "companies": sheets.get("enrichment_output_worksheet", "CompaniesTest"),
        "company_source": sheets.get("company_sheet", {}).get("worksheet", "Company"),
        "keywords": cfg.get("scraping", {}).get("keywords_source", {}).get("worksheet", "Keywords"),
        "spreadsheet_id": sheets.get("spreadsheet_id", ""),
    }
