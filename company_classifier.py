"""
company_classifier.py — Organization-type classification (Task #5).

Separates the organizations in a worksheet into:

    Company                — for-profit business (default)
    University             — degree-granting universities
    Educational Institution— colleges, schools, academies, polytechnics
    Government             — government departments / agencies / public bodies
    Nonprofit / NGO        — foundations, societies, associations, charities
    Hospital / Medical     — hospitals, health systems, medical centers
    Research Institute     — research institutes, national labs
    Other                  — none of the above matched confidently

Classification is rule-based and fully transparent: each verdict comes with a
short reason, derived (in priority order) from the career-page / LinkedIn
domain TLD, the organization name, and an optional Industry column.

The verdict is written into an ``output_column`` (auto-created) on the source
worksheet and to ``logs/<output_csv>``.

Usage:
    python company_classifier.py --config config.yaml
    python company_classifier.py --config config.yaml --source CompaniesTest
    python company_classifier.py --config config.yaml --dry-run
"""

import argparse
import csv
import logging
import os
import re
import sys

import projects_registry
from config_loader import load_config
from google_sheets_store import GoogleSheetsStore
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

# ── Categories ────────────────────────────────────────────────────────────────

COMPANY        = "Company"
UNIVERSITY     = "University"
EDUCATIONAL    = "Educational Institution"
GOVERNMENT     = "Government"
NONPROFIT      = "Nonprofit / NGO"
HOSPITAL       = "Hospital / Medical"
RESEARCH       = "Research Institute"
OTHER          = "Other"

# Row background colors (so the sheet is scannable at a glance).
CATEGORY_COLORS = {
    UNIVERSITY:  {"red": 0.78, "green": 0.86, "blue": 1.00},  # blue
    EDUCATIONAL: {"red": 0.82, "green": 0.92, "blue": 0.99},  # light blue
    GOVERNMENT:  {"red": 0.85, "green": 0.82, "blue": 0.92},  # purple
    NONPROFIT:   {"red": 0.85, "green": 0.95, "blue": 0.83},  # green
    HOSPITAL:    {"red": 1.00, "green": 0.90, "blue": 0.81},  # peach
    RESEARCH:    {"red": 0.98, "green": 0.95, "blue": 0.80},  # yellow
    COMPANY:     None,                                        # white (the norm)
    OTHER:       {"red": 0.93, "green": 0.93, "blue": 0.93},  # grey
}

# ── Keyword signals (matched against the lowercased name) ─────────────────────
# Order of the checks in classify_organization() defines precedence.

_UNIVERSITY_KW = (
    "university", "universidad", "université", "universität", "universiteit",
    "institute of technology", "polytechnic university",
)
_EDUCATIONAL_KW = (
    "college", "school of", "polytechnic", "academy", "académie", "seminary",
    "institut", "high school", "education",
)
_HOSPITAL_KW = (
    "hospital", "medical center", "medical centre", "health system",
    "healthcare", "health care", "clinic", "cancer center", "infirmary",
    "va medical", "va health",
)
_GOVERNMENT_KW = (
    "department of", "ministry of", "city of", "county of", "state of",
    "u.s. ", "united states ", "national institutes of health",
    "administration", "bureau of", "agency", "municipal", "government",
    "veterans affairs", "armed forces", "defense", "federal",
)
_NONPROFIT_KW = (
    "foundation", "non-profit", "nonprofit", " ngo", "society", "association",
    "charity", "charitable", "trust", "council", "institute of public",
    "red cross", "alliance", "coalition",
)
_RESEARCH_KW = (
    "research institute", "research center", "research centre", "national laboratory",
    "national lab", "institutes of health", "max planck", "research foundation",
    "biomedical research",
)   # NB: bare "laboratories"/"labs" excluded — too common in for-profit names
    #     (Abbott Laboratories, Bio-Rad Laboratories, …)
# Strong for-profit suffixes / words.
_COMPANY_KW = (
    " inc", " inc.", " llc", " ltd", " ltd.", " corp", " corporation", " gmbh",
    " co.", " company", " plc", " ag", " s.a.", " pvt", " technologies",
    " pharmaceuticals", " pharma", " biosciences", " therapeutics", " labs",
    " biotech", " systems", " solutions", " sciences", " diagnostics",
)

# ── Industry-column hints (optional, lowercased substring match) ──────────────
_INDUSTRY_HINTS = [
    (UNIVERSITY,  ("higher education", "university")),
    (EDUCATIONAL, ("education", "e-learning", "primary/secondary")),
    (GOVERNMENT,  ("government", "public policy", "legislative", "military", "defense")),
    (NONPROFIT,   ("non-profit", "nonprofit", "philanthropy", "civic")),
    (HOSPITAL,    ("hospital", "health care", "healthcare", "medical practice")),
    (RESEARCH,    ("research",)),
]


def _domain(url: str) -> str:
    """Extract a bare domain from a URL or domain-ish string ('' if none)."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0]


def _any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def classify_organization(
    name: str,
    career_page: str = "",
    linkedin_url: str = "",
    industry: str = "",
) -> tuple:
    """Return (category, reason) for one organization.

    Precedence: domain TLD → name keywords → industry hint → default Company.
    """
    name_l = f" {(name or '').lower().strip()} "
    domain = _domain(career_page) or _domain(linkedin_url)
    ind = (industry or "").lower().strip()

    # 1) Domain TLD — the strongest, hardest-to-fake signal.
    if domain:
        if domain.endswith(".edu") or ".edu." in domain or ".ac." in domain:
            cat = UNIVERSITY if _any(name_l, _UNIVERSITY_KW) else EDUCATIONAL
            return cat, f"domain '{domain}' is academic (.edu/.ac)"
        if domain.endswith(".gov") or ".gov." in domain or domain.endswith(".mil"):
            return GOVERNMENT, f"domain '{domain}' is government (.gov/.mil)"

    # 2) Name keywords (most specific first).
    if _any(name_l, _UNIVERSITY_KW):
        return UNIVERSITY, "name contains a university keyword"
    if _any(name_l, _HOSPITAL_KW):
        return HOSPITAL, "name contains a hospital/medical keyword"
    if _any(name_l, _RESEARCH_KW):
        return RESEARCH, "name contains a research-institute keyword"
    if _any(name_l, _EDUCATIONAL_KW):
        return EDUCATIONAL, "name contains an education keyword"
    if _any(name_l, _GOVERNMENT_KW):
        return GOVERNMENT, "name contains a government keyword"
    if _any(name_l, _NONPROFIT_KW):
        return NONPROFIT, "name contains a nonprofit keyword"
    if _any(name_l, _COMPANY_KW):
        return COMPANY, "name has a for-profit suffix"

    # 3) Industry column hint (when available).
    if ind:
        for cat, hints in _INDUSTRY_HINTS:
            if _any(ind, hints):
                return cat, f"industry '{industry}' hint"

    # 4) .org is a weak nonprofit signal — only used as a last resort.
    if domain.endswith(".org"):
        return NONPROFIT, f"domain '{domain}' is .org (weak)"

    # 5) Default: assume a for-profit company.
    return COMPANY, "default (no academic/gov/nonprofit signal)"


# ── Sheet runner ──────────────────────────────────────────────────────────────

def classify_sheet(
    store: GoogleSheetsStore,
    worksheet: str,
    name_col: str,
    output_col: str,
    career_col: str = "",
    linkedin_col: str = "",
    industry_col: str = "",
    write: bool = True,
    color_rows: bool = True,
) -> dict:
    """Classify every org in ``worksheet`` and write the verdict into ``output_col``.

    Returns a summary dict {category: count}. ``write=False`` is a dry run.
    """
    rows = store.load_all_rows(worksheet)
    if not rows:
        logger.warning(f"No data in '{worksheet}'")
        return {}

    header = rows[0]
    if name_col not in header:
        logger.error(f"Name column '{name_col}' not in header: {header}")
        return {}

    def idx(col):
        return header.index(col) if col and col in header else -1

    ni, ci, li, ii = idx(name_col), idx(career_col), idx(linkedin_col), idx(industry_col)
    logger.info(
        f"Classifying '{worksheet}' | name='{name_col}' "
        f"career={'yes' if ci >= 0 else 'no'} linkedin={'yes' if li >= 0 else 'no'} "
        f"industry={'yes' if ii >= 0 else 'no'} | write={write}"
    )

    out_pos = store.ensure_column(output_col, worksheet)   # 1-indexed
    out_idx = out_pos - 1

    def cell(row, i):
        return row[i].strip() if 0 <= i < len(row) else ""

    summary: dict = {}
    report_rows = []
    column_values = []     # one [value] per data row, for a single batched write
    row_colors = []

    for row_num, row in enumerate(rows[1:], start=2):
        name = cell(row, ni)
        if not name:
            column_values.append([cell(row, out_idx)])   # keep existing (blank)
            continue
        cat, reason = classify_organization(
            name,
            career_page=cell(row, ci),
            linkedin_url=cell(row, li),
            industry=cell(row, ii),
        )
        summary[cat] = summary.get(cat, 0) + 1
        column_values.append([cat])
        row_colors.append((row_num, CATEGORY_COLORS.get(cat)))
        report_rows.append({"Company": name, "Organization Type": cat, "Reason": reason})
        logger.debug(f"  Row {row_num}: {name!r} → {cat}  ({reason})")

    total = sum(summary.values())
    logger.info(
        f"Classified {total} organizations — "
        + "  ".join(f"{k}={v}" for k, v in sorted(summary.items(), key=lambda x: -x[1]))
    )

    _write_csv(report_rows, "logs/classification_latest.csv")

    if write and column_values:
        logger.info(f"Writing {len(column_values)} verdicts into '{output_col}' …")
        store.write_column_values(out_pos, column_values, worksheet, start_row=2)
        if color_rows:
            logger.info("Applying category row colors …")
            try:
                store.batch_format_rows(row_colors, num_cols=len(header), worksheet_name=worksheet)
            except Exception as exc:
                logger.warning(f"Row coloring failed: {exc}")
    elif not write:
        logger.info("Dry run — no sheet writes performed")

    return summary


def _write_csv(report_rows: list, path: str) -> None:
    if not report_rows:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Company", "Organization Type", "Reason"])
        writer.writeheader()
        writer.writerows(report_rows)
    logger.info(f"Wrote classification report to '{path}'")


def _classification_cfg(config: dict) -> dict:
    c = config.get("classification", {}) or {}
    gs = config.get("google_sheets", {})
    return {
        "source_worksheet": c.get("source_worksheet", gs.get("enrichment_output_worksheet", "CompaniesTest")),
        "name_column":      c.get("name_column", "Company"),
        "career_page_column": c.get("career_page_column", "Career Page"),
        "linkedin_url_column": c.get("linkedin_url_column", "LinkedIn URL"),
        "industry_column":  c.get("industry_column", "Industry"),
        "output_column":    c.get("output_column", "Organization Type"),
        "write_to_sheet":   bool(c.get("write_to_sheet", True)),
        "color_rows":       bool(c.get("color_rows", True)),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify organizations into Company / University / Government / etc."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--source", default=None, help="Worksheet to classify")
    parser.add_argument("--dry-run", action="store_true", help="Classify + log, do not write")
    projects_registry.add_project_argument(parser)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config, name="classify")
    gs_config = config.get("google_sheets", {})
    if not gs_config.get("enabled"):
        logger.error("Google Sheets is not enabled in config.yaml")
        sys.exit(1)

    cfg = _classification_cfg(config)
    source = args.source or cfg["source_worksheet"]
    store = GoogleSheetsStore(gs_config)
    classify_sheet(
        store,
        worksheet=source,
        name_col=cfg["name_column"],
        output_col=cfg["output_column"],
        career_col=cfg["career_page_column"],
        linkedin_col=cfg["linkedin_url_column"],
        industry_col=cfg["industry_column"],
        write=not args.dry_run and cfg["write_to_sheet"],
        color_rows=cfg["color_rows"],
    )
