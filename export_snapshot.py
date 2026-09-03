"""
export_snapshot.py — Dump the Google Sheet tabs to static JSON.

The published dashboard is a static page: it has no server and no Google
credentials. This is what feeds it. The GCP service-account key is only ever
present inside a GitHub Actions run, and the only thing that leaves that run is
the JSON written here — so the key never reaches the browser, the repository,
or GitHub Pages.

Written to site/data/:
    index.json          which tabs exist, when they were captured
    <worksheet>.json    columns + rows for one tab

Usage:
    python export_snapshot.py --config config.yaml --out site/data
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import projects_registry
from config_loader import load_config
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)


def _worksheets(config: dict) -> list:
    """The tabs worth publishing, taken from config so the export follows
    whatever the pipeline is actually pointed at."""
    sheets = config["google_sheets"]
    scraping = config.get("scraping", {})
    names = [
        "Settings",                       # the config the published page displays
        sheets.get("jobs_worksheet"),
        sheets.get("enrichment_output_worksheet"),
        sheets.get("company_sheet", {}).get("worksheet"),
        scraping.get("keywords_source", {}).get("worksheet"),
    ]
    seen, out = set(), []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def export(config: dict, out_dir: str) -> dict:
    # Imported here so --help works without Google libraries installed.
    import remote_store
    from web.sheets_data import _unwrap

    store = remote_store.store_for(config)
    os.makedirs(out_dir, exist_ok=True)
    captured = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "captured_at": captured,
        # Lets the published page link straight to the sheet people edit.
        "spreadsheet_id": config["google_sheets"].get("spreadsheet_id", ""),
        "worksheets": [],
    }

    for name in _worksheets(config):
        try:
            raw = store.load_all_rows(name)
        except Exception as exc:
            logger.warning(f"skipping '{name}': {type(exc).__name__}: {exc}")
            manifest["worksheets"].append(
                {"name": name, "error": f"{type(exc).__name__}: {exc}", "row_count": 0})
            continue

        header = [h.strip() for h in raw[0]] if raw else []
        while header and not header[-1]:      # tabs are padded to their column count
            header.pop()

        rows = []
        for offset, values in enumerate(raw[1:], start=2):
            record, blank = {"_row": offset}, True
            for index, column in enumerate(header):
                if not column:
                    continue
                text, url = _unwrap(values[index] if index < len(values) else "")
                record[column] = text
                if url:
                    record[f"{column}__url"] = url
                if text.strip():
                    blank = False
            if not blank:
                rows.append(record)

        payload = {
            "worksheet": name,
            "columns": [h for h in header if h],
            "rows": rows,
            "row_count": len(rows),
            "captured_at": captured,
        }
        path = os.path.join(out_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        size_kb = os.path.getsize(path) / 1024
        logger.info(f"{name}: {len(rows)} rows, {len(payload['columns'])} columns "
                    f"→ {path} ({size_kb:.0f} KB)")
        manifest["worksheets"].append(
            {"name": name, "row_count": len(rows), "columns": payload["columns"],
             "file": f"{name}.json", "size_kb": round(size_kb, 1)})

    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Export sheet tabs to static JSON")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="site/data")
    projects_registry.add_project_argument(ap)
    args = ap.parse_args()

    config = load_config(args.config)

    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config)
    manifest = export(config, args.out)

    total = sum(w.get("row_count", 0) for w in manifest["worksheets"])
    logger.info(f"Exported {len(manifest['worksheets'])} worksheets, {total} rows total")


if __name__ == "__main__":
    main()
