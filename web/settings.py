"""
web/settings.py — The config.yaml editor behind the dashboard's Settings tab.

config.yaml is the single source of truth for every tool in this project, and
its inline comments are the operator's documentation. Saves therefore go
through ruamel.yaml in round-trip mode: edit a value from the browser and the
comments, ordering and formatting all survive. A PyYAML dump would silently
strip them and leave the customer with an undocumented file.

SCHEMA below is what the UI renders. Each field names a dotted path into the
config, the control to draw, and a plain-language explanation — so settings can
be changed without opening a YAML file or reading code.
"""

import os
import shutil
from datetime import datetime

from ruamel.yaml import YAML

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
BACKUP_DIR = os.path.join(PROJECT_ROOT, "logs", "config_backups")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096          # never re-wrap long comment lines
_yaml.indent(mapping=2, sequence=4, offset=2)   # match the file's existing style


# ── What the Settings tab shows ───────────────────────────────────────────────
# group  : heading in the UI
# path   : dotted path into config.yaml
# type   : bool | int | float | text | select | multiselect | keywords
# danger : true → the control is visually flagged as production-affecting

SCHEMA = [
    {
        "group": "Target sheets",
        "help": "Which tabs the automation reads and writes. Point these at the "
                "*_Test tabs while trying things out — the production tabs hold "
                "hand-curated data.",
        "fields": [
            {"path": "google_sheets.jobs_worksheet", "label": "Jobs tab (written to)",
             "type": "select", "options": ["Jobs_Test", "Jobs"], "danger": True,
             "help": "Scraped jobs and validation statuses land here. 'Jobs' is production."},
            {"path": "google_sheets.enrichment_output_worksheet", "label": "Company enrichment tab (written to)",
             "type": "select", "options": ["CompaniesTest", "CompaniesTest2", "Companies"], "danger": True,
             "help": "Where the enricher writes employee count / career page / LinkedIn URL."},
            {"path": "classification.source_worksheet", "label": "Classification tab",
             "type": "select", "options": ["CompaniesTest", "CompaniesTest2", "Company"], "danger": True,
             "help": "Which organisation list gets an 'Organization Type' verdict."},
            {"path": "google_sheets.company_sheet.worksheet", "label": "Company source tab (read only)",
             "type": "text",
             "help": "The hand-maintained source of truth. Automation never writes here."},
            {"path": "google_sheets.enabled", "label": "Write to Google Sheets",
             "type": "bool", "danger": True,
             "help": "Off = local CSV only. Useful for a completely side-effect-free trial run."},
        ],
    },
    {
        "group": "Search keywords",
        "help": "Keywords drive every scrape. Reading them from the sheet means "
                "they can be changed without touching code or redeploying.",
        "fields": [
            {"path": "scraping.keywords_source.mode", "label": "Read keywords from",
             "type": "select", "options": ["sheet", "file"],
             "help": "'sheet' reads the Keywords tab live; 'file' uses keywords.txt."},
            {"path": "scraping.keywords_source.worksheet", "label": "Keywords tab name", "type": "text"},
            {"path": "scraping.keywords_source.column", "label": "Keywords column header", "type": "text"},
            {"path": "scraping.keywords_fallback_file", "label": "Fallback keywords file", "type": "text",
             "help": "Used when the sheet is unreachable or empty, so a scrape never runs with zero keywords."},
        ],
    },
    {
        "group": "Job boards",
        "help": "Which platforms to search. The blocked ones sit behind Cloudflare "
                "or render jobs in JavaScript; enabling them produces zero rows, "
                "not more rows.",
        "fields": [
            {"path": "scraping.platforms", "label": "Active platforms", "type": "multiselect",
             "options": [
                 {"value": "linkedin", "label": "LinkedIn", "note": "works — 10 jobs/page"},
                 {"value": "indeed", "label": "Indeed", "note": "works"},
                 {"value": "internshala", "label": "Internshala", "note": "works"},
                 {"value": "jobs.lever", "label": "Lever", "note": "needs company slugs"},
                 {"value": "glassdoor", "label": "Glassdoor", "note": "blocked — Cloudflare 403"},
                 {"value": "wellfound", "label": "Wellfound", "note": "blocked — Cloudflare 403"},
                 {"value": "simplyhired", "label": "SimplyHired", "note": "blocked — Cloudflare 403"},
                 {"value": "ycombinator", "label": "Y Combinator", "note": "blocked — JS-rendered"},
             ]},
        ],
    },
    {
        "group": "LinkedIn depth",
        "help": "How far past the first page of results to go. LinkedIn's guest "
                "endpoint returns 10 jobs per fetch and treats 'start' as a row "
                "offset — page size must stay 10 or jobs get skipped.",
        "fields": [
            {"path": "scraping.platform_settings.linkedin.max_pages", "label": "Pages per keyword",
             "type": "int", "min": 1, "max": 50,
             "help": "12 pages ≈ 120 jobs per keyword. Higher = slower and more requests."},
            {"path": "scraping.platform_settings.linkedin.page_size", "label": "Jobs per page",
             "type": "int", "min": 1, "max": 25, "danger": True,
             "help": "Leave at 10. This must match what the endpoint actually returns — "
                     "a mismatch silently skips whole blocks of jobs."},
            {"path": "scraping.platform_settings.linkedin.page_delay_seconds", "label": "Pause between pages (s)",
             "type": "float", "min": 0, "max": 30},
            {"path": "scraping.platform_settings.linkedin.location", "label": "Location", "type": "text"},
        ],
    },
    {
        "group": "Job validation",
        "help": "Probes every job link and writes Active / Expired / Removed / "
                "Unknown, then optionally deletes the rows it condemned.",
        "fields": [
            {"path": "job_validation.re_validate", "label": "Re-check jobs that already have a status",
             "type": "bool",
             "help": "Off = only fill in blanks (fast). On = re-probe every link, "
                     "which is how an Active job becomes Expired over time."},
            {"path": "job_validation.remove_rows", "label": "Delete rows after validating",
             "type": "bool", "danger": True,
             "help": "Off = statuses are written and rows are only coloured. On = rows "
                     "with the statuses below are deleted from the sheet after every "
                     "validation run. Deleted rows are always backed up to a CSV in "
                     "logs/ first."},
            {"path": "job_validation.remove_statuses", "label": "Statuses to delete",
             "type": "multiselect", "danger": True,
             "options": [
                 {"value": "Expired", "label": "Expired", "note": "closed / 4xx"},
                 {"value": "Removed", "label": "Removed", "note": "404 / 410 — gone"},
                 {"value": "Unknown", "label": "Unknown", "note": "network error — usually keep"},
                 {"value": "Active", "label": "Active", "note": "live — rarely what you want"},
             ],
             "help": "Only used when the switch above is on. Unknown means the probe "
                     "failed, not that the job did — deleting those loses live jobs."},
        ],
    },
    {
        "group": "Career-page scraping",
        "fields": [
            {"path": "career_pages.source_worksheet", "label": "Company list tab", "type": "text"},
            {"path": "career_pages.max_jobs_per_company", "label": "Max jobs per company",
             "type": "int", "min": 1, "max": 500,
             "help": "Safety cap so one huge careers site cannot dominate a run."},
            {"path": "career_pages.keyword_match_mode", "label": "Keyword strictness",
             "type": "select", "options": ["all", "most", "any"],
             "help": "How much of a multi-word keyword must appear in the job title. "
                     "'all' is precise but strict — a run over 325 companies kept 9 "
                     "postings out of thousands scraped. 'most' loosens it; 'any' "
                     "matches on one word and brings a lot of noise."},
        ],
    },
    {
        "group": "Organisation classification",
        "fields": [
            {"path": "classification.write_to_sheet", "label": "Write verdicts to the sheet", "type": "bool",
             "help": "Off = classify and log only, nothing is written. Same as --dry-run."},
            {"path": "classification.color_rows", "label": "Colour rows by category", "type": "bool"},
            {"path": "classification.output_column", "label": "Verdict column header", "type": "text"},
        ],
    },
    {
        "group": "Pagination analysis",
        "help": "Read-only diagnostic: measures how many jobs sit behind "
                "'See More Jobs' / infinite scroll. Never writes to the Jobs tab.",
        "fields": [
            {"path": "pagination_analysis.max_probe_pages", "label": "Max pages to probe",
             "type": "int", "min": 1, "max": 200,
             "help": "50 pages × 10 jobs = up to 500 jobs probed per keyword."},
            {"path": "pagination_analysis.keywords_limit", "label": "Keywords to probe (0 = all)",
             "type": "int", "min": 0, "max": 100},
            {"path": "pagination_analysis.page_delay_seconds", "label": "Pause between pages (s)",
             "type": "float", "min": 0, "max": 30},
            {"path": "pagination_analysis.write_to_sheet", "label": "Also write the report to a sheet tab",
             "type": "bool"},
        ],
    },
    {
        "group": "Network & logging",
        "fields": [
            {"path": "http.timeout_seconds", "label": "Request timeout (s)", "type": "int", "min": 1, "max": 120},
            {"path": "http.max_retries", "label": "Retries on failure", "type": "int", "min": 0, "max": 10},
            {"path": "http.delay_between_requests_seconds", "label": "Pause between requests (s)",
             "type": "float", "min": 0, "max": 30,
             "help": "Raise this if a platform starts rate-limiting. 0 = no pause."},
            {"path": "logging.level", "label": "Log detail", "type": "select",
             "options": ["debug", "info", "warning", "error"],
             "help": "'debug' shows every request in the live console."},
        ],
    },
]


# ── Dotted-path access ────────────────────────────────────────────────────────

def get_path(data, path: str, default=None):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(data, path: str, value) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def load_raw():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return _yaml.load(fh)


def describe() -> dict:
    """The schema with each field's current value filled in, for the UI."""
    cfg = load_raw()
    groups = []
    for group in SCHEMA:
        fields = []
        for field in group["fields"]:
            item = dict(field)
            value = get_path(cfg, field["path"])
            if field["type"] in ("multiselect", "keywords") and value is not None:
                value = list(value)
            item["value"] = value
            fields.append(item)
        groups.append({"group": group["group"], "help": group.get("help"), "fields": fields})
    return {"groups": groups, "path": os.path.relpath(CONFIG_PATH, PROJECT_ROOT)}


def _coerce(field: dict, value):
    """Turn a JSON value from the browser into the type config.yaml expects."""
    kind = field["type"]
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind in ("multiselect", "keywords"):
        return [str(v) for v in (value or [])]
    return str(value)


def save(updates: dict) -> dict:
    """Apply {path: value} to config.yaml, keeping every comment intact.

    The previous file is copied into logs/config_backups first, so a bad edit
    from the browser is always one file-copy away from being undone.
    """
    known = {f["path"]: f for group in SCHEMA for f in group["fields"]}
    unknown = [p for p in updates if p not in known]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")

    cfg = load_raw()
    applied = {}
    for path, value in updates.items():
        coerced = _coerce(known[path], value)
        if get_path(cfg, path) != coerced:
            set_path(cfg, path, coerced)
            applied[path] = coerced

    if not applied:
        return {"changed": {}, "backup": None}

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, f"config.{datetime.now():%Y%m%d_%H%M%S}.yaml")
    shutil.copy2(CONFIG_PATH, backup)

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _yaml.dump(cfg, fh)
    os.replace(tmp, CONFIG_PATH)
    return {"changed": applied, "backup": os.path.relpath(backup, PROJECT_ROOT)}
