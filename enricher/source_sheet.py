"""Source-sheet readers for company enrichment."""

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

_LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:company|school)/[^/?#]+",
    re.IGNORECASE,
)


def _extract_linkedin_company_url(raw_url: str) -> str:
    """Extract canonical LinkedIn company/school URL from a job-link style URL."""
    if not raw_url:
        return ""

    candidates = [raw_url]
    try:
        parsed = urlparse(raw_url)
        for values in parse_qs(parsed.query).values():
            for value in values:
                if value and "http" in value:
                    candidates.append(value)
                    candidates.append(unquote(value))
                    candidates.append(unquote(unquote(value)))
    except Exception:
        pass

    for item in candidates:
        if not item:
            continue
        text = unquote(unquote(item))
        match = _LINKEDIN_COMPANY_RE.search(text)
        if not match:
            continue
        found = match.group(0).rstrip("/") + "/"
        found = re.sub(
            r"^https?://([a-z]{2,3}\.)?www\.linkedin\.com",
            "https://www.linkedin.com",
            found,
            flags=re.IGNORECASE,
        )
        return found

    return ""


def _col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to A1-notation letter (A, B, ..., AA, ...)."""
    result = ""
    n = idx + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_jobs_company_linkedin(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    company_header: str = "Company",
    linkedin_header: str = "Linkedin-Url",
    job_link_header: str = "Job Link",
) -> dict:
    """Read source company -> LinkedIn URL mapping from configured source sheet.

    Fallback order per row:
    1. Explicit LinkedIn URL column value
    2. LinkedIn company/school URL extracted from Job Link column
    3. Hyperlink attached to Company cell (legacy fallback)
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        header_resp = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!1:1")
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Could not read Jobs tab header: {exc}")
        return {}

    header_row = header_resp.get("values", [[]])
    header_row = header_row[0] if header_row else []
    try:
        company_col_idx = header_row.index(company_header)
    except ValueError:
        logger.warning(
            f"No '{company_header}' column found in '{sheet_name}' header"
        )
        return {}

    linkedin_col_idx = None
    try:
        linkedin_col_idx = header_row.index(linkedin_header)
    except ValueError:
        logger.debug(
            f"No '{linkedin_header}' column in '{sheet_name}', falling back to company-cell hyperlinks"
        )

    job_link_col_idx = None
    try:
        job_link_col_idx = header_row.index(job_link_header)
    except ValueError:
        logger.debug(
            f"No '{job_link_header}' column in '{sheet_name}', job-link fallback unavailable"
        )

    if linkedin_col_idx is not None or job_link_col_idx is not None:
        company_col_letter = _col_index_to_letter(company_col_idx)
        company_range = f"'{sheet_name}'!{company_col_letter}2:{company_col_letter}"
        ranges = [company_range]
        linkedin_pos = None
        job_link_pos = None
        if linkedin_col_idx is not None:
            linkedin_col_letter = _col_index_to_letter(linkedin_col_idx)
            ranges.append(f"'{sheet_name}'!{linkedin_col_letter}2:{linkedin_col_letter}")
            linkedin_pos = len(ranges) - 1
        if job_link_col_idx is not None:
            job_link_col_letter = _col_index_to_letter(job_link_col_idx)
            ranges.append(f"'{sheet_name}'!{job_link_col_letter}2:{job_link_col_letter}")
            job_link_pos = len(ranges) - 1

        try:
            result = (
                sheets_svc.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=ranges,
                )
                .execute()
            )
        except HttpError as exc:
            logger.error(f"Sheets API call failed reading source LinkedIn values: {exc}")
            return {}

        value_ranges = result.get("valueRanges", [])
        company_values = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
        linkedin_values = []
        if linkedin_pos is not None and linkedin_pos < len(value_ranges):
            linkedin_values = value_ranges[linkedin_pos].get("values", [])
        job_link_values = []
        if job_link_pos is not None and job_link_pos < len(value_ranges):
            job_link_values = value_ranges[job_link_pos].get("values", [])

        data = {}
        for i, name_row in enumerate(company_values):
            company_name = (name_row[0] if name_row else "").strip()
            linkedin_row = linkedin_values[i] if i < len(linkedin_values) else []
            linkedin_url = (linkedin_row[0] if linkedin_row else "").strip()
            job_link_row = job_link_values[i] if i < len(job_link_values) else []
            job_link_url = (job_link_row[0] if job_link_row else "").strip()

            resolved_linkedin = linkedin_url or _extract_linkedin_company_url(job_link_url)

            if company_name and company_name not in data:
                data[company_name] = resolved_linkedin
                logger.debug(
                    "Source map linkedin | sheet='%s' company='%s' linkedin='%s' raw_linkedin='%s' job_link='%s'",
                    sheet_name,
                    company_name,
                    resolved_linkedin,
                    linkedin_url,
                    job_link_url,
                )
        return data

    col_letter = _col_index_to_letter(company_col_idx)
    range_notation = f"'{sheet_name}'!{col_letter}2:{col_letter}"

    try:
        result = (
            sheets_svc.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[range_notation],
                includeGridData=True,
                fields=(
                    "sheets.data.rowData.values.hyperlink,"
                    "sheets.data.rowData.values.formattedValue"
                ),
            )
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Sheets API call failed reading Jobs company column: {exc}")
        return {}

    rows = (
        result.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    data = {}
    for row in rows:
        cells = row.get("values", [])
        if not cells:
            continue
        cell = cells[0]
        name = cell.get("formattedValue", "").strip()
        url = cell.get("hyperlink", "").strip()
        if name and name not in data:
            data[name] = url
            logger.debug(
                "Source map linkedin (hyperlink fallback) | sheet='%s' company='%s' linkedin='%s'",
                sheet_name,
                name,
                url,
            )

    return data


def get_source_sheet_career_pages(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    company_header: str = "Company",
    career_page_header: str = "Career-Page",
) -> dict:
    """Read company name -> career page URL mapping from the source sheet."""
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        header_resp = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!1:1")
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Could not read {sheet_name} header: {exc}")
        return {}

    header_row = header_resp.get("values", [[]])
    header_row = header_row[0] if header_row else []

    try:
        company_col_idx = header_row.index(company_header)
    except ValueError:
        logger.warning(f"No '{company_header}' column in '{sheet_name}'")
        return {}

    try:
        career_col_idx = header_row.index(career_page_header)
    except ValueError:
        logger.debug(f"No '{career_page_header}' column in '{sheet_name}'")
        return {}

    company_col_letter = _col_index_to_letter(company_col_idx)
    career_col_letter = _col_index_to_letter(career_col_idx)
    company_range = f"'{sheet_name}'!{company_col_letter}2:{company_col_letter}"
    career_range = f"'{sheet_name}'!{career_col_letter}2:{career_col_letter}"

    try:
        result = (
            sheets_svc.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=[company_range, career_range],
            )
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Sheets API call failed reading career pages: {exc}")
        return {}

    value_ranges = result.get("valueRanges", [])
    company_values = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
    career_values = value_ranges[1].get("values", []) if len(value_ranges) > 1 else []

    data = {}
    for i, name_row in enumerate(company_values):
        company_name = (name_row[0] if name_row else "").strip()
        career_row = career_values[i] if i < len(career_values) else []
        career_page = (career_row[0] if career_row else "").strip()
        if company_name and company_name not in data and career_page:
            data[company_name] = career_page
            logger.debug(
                "Source map career | sheet='%s' company='%s' career='%s'",
                sheet_name,
                company_name,
                career_page,
            )

    return data


def get_source_sheet_employee_counts(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    company_header: str = "Company",
    employee_count_header: str = "Employee-Count",
) -> dict:
    """Read company name -> employee count mapping from the source sheet."""
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        header_resp = (
            sheets_svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!1:1")
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Could not read {sheet_name} header: {exc}")
        return {}

    header_row = header_resp.get("values", [[]])
    header_row = header_row[0] if header_row else []

    try:
        company_col_idx = header_row.index(company_header)
    except ValueError:
        logger.warning(f"No '{company_header}' column in '{sheet_name}'")
        return {}

    try:
        employee_col_idx = header_row.index(employee_count_header)
    except ValueError:
        logger.debug(f"No '{employee_count_header}' column in '{sheet_name}'")
        return {}

    company_col_letter = _col_index_to_letter(company_col_idx)
    employee_col_letter = _col_index_to_letter(employee_col_idx)
    company_range = f"'{sheet_name}'!{company_col_letter}2:{company_col_letter}"
    employee_range = f"'{sheet_name}'!{employee_col_letter}2:{employee_col_letter}"

    try:
        result = (
            sheets_svc.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=[company_range, employee_range],
            )
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Sheets API call failed reading employee counts: {exc}")
        return {}

    value_ranges = result.get("valueRanges", [])
    company_values = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
    employee_values = value_ranges[1].get("values", []) if len(value_ranges) > 1 else []

    data = {}
    for i, name_row in enumerate(company_values):
        company_name = (name_row[0] if name_row else "").strip()
        employee_row = employee_values[i] if i < len(employee_values) else []
        employee_count = (employee_row[0] if employee_row else "").strip()
        if company_name and company_name not in data and employee_count:
            data[company_name] = employee_count
            logger.debug(
                "Source map employee | sheet='%s' company='%s' employee='%s'",
                sheet_name,
                company_name,
                employee_count,
            )

    return data
