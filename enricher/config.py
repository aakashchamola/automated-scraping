"""Configuration helper utilities for company enricher."""


def required_company_columns(gs_config: dict) -> dict:
    """Return configured Companies-sheet headers with safe defaults."""
    cfg = gs_config.get("companies_columns", {}) if isinstance(gs_config, dict) else {}
    return {
        "company": cfg.get("company", "Company"),
        "employee_count": cfg.get("employee_count", "Employee Count"),
        "career_page": cfg.get("career_page", "Career Page"),
        "linkedin_url": cfg.get("linkedin_url", "LinkedIn URL"),
    }


def enrichment_controls(gs_config: dict) -> dict:
    """Return enrichment retry controls."""
    cfg = gs_config.get("enrichment_controls", {}) if isinstance(gs_config, dict) else {}
    raw_invalid = cfg.get("retry_invalid_career_values", ["://"])
    if not isinstance(raw_invalid, list):
        raw_invalid = ["://"]
    invalid_values = {str(v).strip().lower() for v in raw_invalid if str(v).strip()}
    return {
        "retry_na_fields": bool(cfg.get("retry_na_fields", False)),
        "retry_invalid_career_values": invalid_values,
    }


def validation_controls(gs_config: dict) -> dict:
    """Return validation controls with backward compatibility."""
    cfg = gs_config.get("validation", {}) if isinstance(gs_config, dict) else {}
    retry_na = cfg.get("retry_na_fields")
    if retry_na is None:
        enrich_cfg = gs_config.get("enrichment_controls", {}) if isinstance(gs_config, dict) else {}
        retry_na = enrich_cfg.get("retry_na_fields", False)
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "retry_na_fields": bool(retry_na),
    }


def source_sheet_controls(gs_config: dict) -> dict:
    """Return source-sheet controls with backward compatibility."""
    src = gs_config.get("source_sheet", {}) if isinstance(gs_config, dict) else {}
    return {
        "enabled": bool(src.get("enabled", True)),
        "worksheet": src.get(
            "worksheet",
            gs_config.get("company_source_worksheet", gs_config.get("worksheet", "Jobs")),
        ),
        "company_header": src.get(
            "company_header",
            gs_config.get("company_source_header", "Company"),
        ),
        "employee_count_header": src.get("employee_count_header", "Employee-Count"),
        "career_page_header": src.get("career_page_header", "Career-Page"),
        "linkedin_url_header": src.get("linkedin_url_header", "Linkedin-Url"),
        "job_link_header": src.get("job_link_header", "Job Link"),
        "use_for_company_sync": bool(src.get("use_for_company_sync", True)),
        "use_for_linkedin_fallback": bool(src.get("use_for_linkedin_fallback", True)),
        "use_for_career_fallback": bool(src.get("use_for_career_fallback", True)),
    }
