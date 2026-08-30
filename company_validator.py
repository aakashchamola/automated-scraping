"""
company_validator.py — Company database mismatch checker.

Compares the manually-curated Company sheet (source of truth) against the
automation-generated enrichment sheet (CompaniesTest) and flags cells where
the automation wrote something that disagrees with the manual data.

A cell is flagged (colored orange) when:
  - The enrichment value differs from the Company sheet value, AND
  - Neither side is blank / N/A.

Fields compared:
  Company sheet           →  CompaniesTest
  Employee-Count          →  Employee Count
  Career-Page             →  Career Page
  Linkedin-Url            →  LinkedIn URL

Usage:
    python company_validator.py
    python company_validator.py --config config.yaml
    python company_validator.py --source Company --enrichment CompaniesTest
"""

import argparse
import logging
import sys

import projects_registry
from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

# ── Colors ────────────────────────────────────────────────────────────────────

_COLOR_ORANGE = {"red": 1.0, "green": 0.78, "blue": 0.4}   # mismatch

# ── Field mapping: (Company-sheet column, CompaniesTest column) ───────────────
# Fallback only — the real mapping is built from config in __main__ via
# build_field_map() so that renamed columns (e.g. "Avg. Employee-Count") are
# always picked up without editing this file.
_FIELD_MAP = [
    ("Avg. Employee-Count", "Employee Count"),
    ("Career-Page",         "Career Page"),
    ("Linkedin-Url",        "LinkedIn URL"),
]

_NA_VALUES = {"", "n/a", "na", "none", "null", "-", "n/a (no public career page)"}


def build_field_map(gs_config: dict) -> list:
    """Build [(source_col, enrich_col), ...] from config.

    Source columns come from ``google_sheets.company_sheet`` and enrichment
    columns from ``google_sheets.enrichment_output_columns`` so the comparison
    survives column renames without code edits.
    """
    company = gs_config.get("company_sheet", {})
    enrich = gs_config.get("enrichment_output_columns", {})
    pairs = [
        (company.get("employee_count_column"), enrich.get("employee_count")),
        (company.get("career_page_column"),    enrich.get("career_page")),
        (company.get("linkedin_url_column"),   enrich.get("linkedin_url")),
    ]
    field_map = [(s, e) for s, e in pairs if s and e]
    return field_map or list(_FIELD_MAP)


def _is_na(value: str) -> bool:
    return value.strip().lower() in _NA_VALUES


def _normalize(value: str, field: str) -> str:
    """Normalize a value before comparison so that cosmetic differences
    (thousands separators, URL scheme, www., trailing slash) don't count as
    mismatches — only genuine disagreements are flagged.

    ``field`` is the source column name; we infer its kind from it:
      - contains "count"           → numeric  (keep digits only)
      - contains "url"/"page"/"link" → url     (drop scheme/www./trailing slash)
      - otherwise                  → plain text (lowercased, collapsed spaces)
    """
    v = (value or "").strip()
    f = field.lower()
    if "count" in f:
        digits = "".join(ch for ch in v if ch.isdigit())
        return digits  # "104,832" -> "104832", "1,000+" -> "1000"
    if any(k in f for k in ("url", "page", "link")):
        v = v.lower()
        for scheme in ("https://", "http://"):
            if v.startswith(scheme):
                v = v[len(scheme):]
        if v.startswith("www."):
            v = v[4:]
        return v.rstrip("/")
    return " ".join(v.lower().split())


# ── Core logic ────────────────────────────────────────────────────────────────

def flag_mismatches(
    sheet_store: GoogleSheetsStore,
    source_ws: str,
    source_company_col: str,
    enrich_ws: str,
    enrich_company_col: str,
    field_map: list = None,
) -> dict:
    """Compare source (Company) vs enrichment (CompaniesTest) and color mismatches.

    ``field_map`` is a list of (source_column, enrichment_column) tuples; when
    omitted it falls back to the module-level ``_FIELD_MAP``.

    Returns a summary dict: {source_field: mismatch_count}.
    """
    field_map = field_map or list(_FIELD_MAP)
    logger.info(
        f"Mismatch check | source='{source_ws}' vs enrichment='{enrich_ws}' | "
        f"fields={[f'{s}->{e}' for s, e in field_map]}"
    )

    source_rows = sheet_store.load_all_rows(source_ws)
    enrich_rows = sheet_store.load_all_rows(enrich_ws)

    if not source_rows:
        logger.error(f"Source sheet '{source_ws}' is empty or missing")
        return {}
    if not enrich_rows:
        logger.error(f"Enrichment sheet '{enrich_ws}' is empty or missing")
        return {}

    source_header = source_rows[0]
    enrich_header = enrich_rows[0]

    if source_company_col not in source_header:
        logger.error(f"Column '{source_company_col}' not found in '{source_ws}': {source_header}")
        return {}
    if enrich_company_col not in enrich_header:
        logger.error(f"Column '{enrich_company_col}' not found in '{enrich_ws}': {enrich_header}")
        return {}

    # Build lookup from Company sheet: {name_lower → {field: value}}
    src_ci = source_header.index(source_company_col)
    source_map: dict = {}
    for row in source_rows[1:]:
        if src_ci >= len(row):
            continue
        name = row[src_ci].strip()
        if not name:
            continue
        source_map[name.lower()] = {
            src_field: (
                row[source_header.index(src_field)].strip()
                if src_field in source_header and source_header.index(src_field) < len(row)
                else ""
            )
            for src_field, _ in field_map
        }

    logger.info(
        f"Loaded {len(source_map)} companies from '{source_ws}' | "
        f"{len(enrich_rows) - 1} rows in '{enrich_ws}'"
    )

    enrich_ci = enrich_header.index(enrich_company_col)
    mismatch_cells: list = []
    summary: dict = {src_f: 0 for src_f, _ in field_map}
    checked = 0

    for row_num, row in enumerate(enrich_rows[1:], start=2):
        if enrich_ci >= len(row):
            continue
        name = row[enrich_ci].strip()
        if not name:
            continue

        src_data = source_map.get(name.lower())
        if not src_data:
            logger.debug(f"  '{name}' not found in source sheet — skipping")
            continue

        checked += 1
        for src_field, enrich_field in field_map:
            if enrich_field not in enrich_header:
                continue
            col_idx = enrich_header.index(enrich_field)
            enrich_val = row[col_idx].strip() if col_idx < len(row) else ""
            src_val    = src_data.get(src_field, "")

            # Skip when either side is N/A / blank — nothing meaningful to compare
            if _is_na(enrich_val) or _is_na(src_val):
                continue

            # Compare normalized values so commas / URL scheme / www. don't
            # count as mismatches — only real disagreements are flagged.
            if _normalize(enrich_val, src_field) != _normalize(src_val, src_field):
                mismatch_cells.append((row_num, col_idx + 1, _COLOR_ORANGE))
                summary[src_field] += 1
                logger.debug(
                    f"  MISMATCH | {name!r} | {src_field}: "
                    f"source={src_val!r}  enriched={enrich_val!r}"
                )

    total = sum(summary.values())
    logger.info(
        f"Compared {checked} matched companies | "
        f"{total} mismatched cells — "
        + "  ".join(f"{ef}={summary[sf]}" for sf, ef in field_map)
    )

    if mismatch_cells:
        logger.info(f"Coloring {len(mismatch_cells)} mismatched cells orange in '{enrich_ws}'…")
        try:
            sheet_store.batch_format_cells(mismatch_cells, worksheet_name=enrich_ws)
            logger.info("Done.")
        except Exception as exc:
            logger.warning(f"Cell formatting failed: {exc}")
    else:
        logger.info("No mismatches found — nothing to color.")

    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flag mismatches between the Company sheet and the enrichment output sheet"
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--source",
        default=None,
        help="Source (manual) worksheet name (default: google_sheets.company_sheet.worksheet)",
    )
    parser.add_argument(
        "--enrichment",
        default=None,
        help="Enrichment output worksheet name (default: google_sheets.enrichment_output_worksheet)",
    )
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config, name="company_validate")
    gs_config = config.get("google_sheets", {})

    if not gs_config.get("enabled"):
        logger.error("Google Sheets is not enabled in config.yaml")
        sys.exit(1)

    company_cfg = gs_config.get("company_sheet", {})
    source_ws        = args.source     or company_cfg.get("worksheet", "Company")
    source_company   = company_cfg.get("company_column", "Company")

    enrich_col_cfg   = gs_config.get("enrichment_output_columns", {})
    enrich_ws        = args.enrichment or gs_config.get("enrichment_output_worksheet", "CompaniesTest")
    enrich_company   = enrich_col_cfg.get("company", "Company")

    field_map = build_field_map(gs_config)
    store = GoogleSheetsStore(gs_config)
    flag_mismatches(
        store, source_ws, source_company, enrich_ws, enrich_company,
        field_map=field_map,
    )
