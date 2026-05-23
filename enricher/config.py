"""Configuration helper utilities for company enricher.

Reads from the google_sheets section of config.yaml and returns typed,
default-filled dicts so the enricher never has to handle missing keys.
"""


def required_company_columns(gs_config: dict) -> dict:
    """Return column names for the enrichment output worksheet.

    Reads from google_sheets.enrichment_output_columns in config.yaml.
    Falls back to the old key "companies_columns" for backward compatibility.
    """
    if not isinstance(gs_config, dict):
        gs_config = {}
    # New key name; fall back to old "companies_columns" key
    cfg = gs_config.get("enrichment_output_columns", gs_config.get("companies_columns", {}))
    return {
        "company":        cfg.get("company",        "Company"),
        "employee_count": cfg.get("employee_count", "Employee Count"),
        "career_page":    cfg.get("career_page",    "Career Page"),
        "linkedin_url":   cfg.get("linkedin_url",   "LinkedIn URL"),
    }


def enrichment_controls(gs_config: dict) -> dict:
    """Return enrichment retry controls.

    Reads from google_sheets.enrichment_controls in config.yaml.
    """
    cfg = gs_config.get("enrichment_controls", {}) if isinstance(gs_config, dict) else {}
    raw_invalid = cfg.get("retry_invalid_career_values", ["://"])
    if not isinstance(raw_invalid, list):
        raw_invalid = ["://"]
    invalid_values = {str(v).strip().lower() for v in raw_invalid if str(v).strip()}
    return {
        "retry_na_fields":              bool(cfg.get("retry_na_fields", False)),
        "retry_invalid_career_values":  invalid_values,
    }


def validation_controls(gs_config: dict) -> dict:
    """Return enrichment validation controls (backward compat wrapper).

    Previously at google_sheets.validation; now merged into enrichment_controls.
    """
    enrich = enrichment_controls(gs_config)
    return {
        "enabled":         False,   # enrichment validation is deprecated
        "retry_na_fields": enrich["retry_na_fields"],
    }


def source_sheet_controls(gs_config: dict) -> dict:
    """Return company sheet read controls.

    Reads from google_sheets.company_sheet in config.yaml.
    The old key (source_sheet) is accepted as a fallback for compatibility.
    """
    if not isinstance(gs_config, dict):
        gs_config = {}

    # New key name; fall back to old "source_sheet" key
    src = gs_config.get("company_sheet", gs_config.get("source_sheet", {}))

    return {
        "enabled": bool(src.get("enabled", True)),

        # Which worksheet to read company data from
        "worksheet": src.get(
            "worksheet",
            gs_config.get("company_source_worksheet", gs_config.get("jobs_worksheet", "Jobs")),
        ),

        # Column header names (hyphenated as they appear in the actual sheet)
        "company_header":        src.get("company_column",         src.get("company_header",         "Company")),
        "employee_count_header": src.get("employee_count_column",  src.get("employee_count_header",  "Employee-Count")),
        "career_page_header":    src.get("career_page_column",     src.get("career_page_header",     "Career-Page")),
        "linkedin_url_header":   src.get("linkedin_url_column",    src.get("linkedin_url_header",    "Linkedin-Url")),
        "job_link_header":       src.get("job_link_column",        src.get("job_link_header",        "Job Link")),

        # What to use this sheet for
        "use_for_company_sync":      bool(src.get("sync_companies_to_enrichment", src.get("use_for_company_sync",      True))),
        "use_for_linkedin_fallback": bool(src.get("use_for_linkedin_hyperlinks",  src.get("use_for_linkedin_fallback", True))),
        "use_for_career_fallback":   bool(src.get("use_for_career_page_fallback", src.get("use_for_career_fallback",   True))),
    }
