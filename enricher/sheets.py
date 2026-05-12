"""Google Sheets and header utilities for company enricher."""

import logging
import re

logger = logging.getLogger(__name__)


def col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to A1-notation letter (A, B, ..., AA, ...)."""
    result = ""
    n = idx + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_column_data(
    sheets_svc,
    spreadsheet_id: str,
    sheet_name: str,
    col_idx: int,
    start_row: int = 2,
) -> dict:
    """Read non-empty column values and attached hyperlinks using Sheets API v4 grid data."""
    col_letter = col_index_to_letter(col_idx)
    range_notation = f"'{sheet_name}'!{col_letter}{start_row}:{col_letter}"
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
    except Exception as exc:
        logger.error(f"Sheets API call failed: {exc}")
        return {}

    rows = (
        result.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    data = {}
    for i, row in enumerate(rows):
        actual_row = i + start_row
        cells = row.get("values", [])
        if not cells:
            continue
        cell = cells[0]
        value = cell.get("formattedValue", "").strip()
        url = cell.get("hyperlink", "").strip()
        if value:
            data[actual_row] = {"value": value, "url": url}
            logger.debug(
                "Sheet column read | sheet='%s' row=%s col=%s value='%s' hyperlink='%s'",
                sheet_name,
                actual_row,
                col_letter,
                value,
                url,
            )

    return data


def header_index_map(header_row: list) -> dict:
    """Case-insensitive header -> index mapping for row 1 values."""
    mapping = {}
    for idx, raw in enumerate(header_row):
        key = (raw or "").strip().lower()
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def normalize_header_key(value: str) -> str:
    """Normalize header labels for tolerant matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def header_aliases(field: str, desired_header: str) -> set:
    """Known synonyms to avoid duplicate semantic columns."""
    desired = (desired_header or "").strip()
    aliases = {desired}
    if field == "company":
        aliases.update({"Company", "Companies"})
    elif field == "employee_count":
        aliases.update({"Employee Count", "Employee-Count"})
    elif field == "career_page":
        aliases.update({"Career Page", "Career-Page"})
    elif field == "linkedin_url":
        aliases.update({"LinkedIn URL", "Linkedin-Url", "LinkedIn-Url", "Linkedin URL"})
    return {alias for alias in aliases if alias}


def ensure_required_headers(comp_ws, required_headers: dict, sheets_call) -> dict:
    """Ensure required headers exist in row 1, creating missing headers at row end."""
    header_values = comp_ws.row_values(1)
    index_by_header = header_index_map(header_values)
    normalized_header_index = {}
    for idx, raw in enumerate(header_values):
        nkey = normalize_header_key(raw)
        if nkey and nkey not in normalized_header_index:
            normalized_header_index[nkey] = idx

    field_indexes = {}
    changed = False

    for field, header in required_headers.items():
        idx = index_by_header.get(header.strip().lower())
        if idx is None:
            for alias in header_aliases(field, header):
                idx = normalized_header_index.get(normalize_header_key(alias))
                if idx is not None:
                    logger.info(
                        f"Using existing header '{header_values[idx]}' for '{field}'"
                    )
                    break
        if idx is None:
            idx = len(header_values)
            header_values.append(header)
            index_by_header[header.strip().lower()] = idx
            normalized_header_index[normalize_header_key(header)] = idx
            changed = True
            logger.info(
                f"Added missing header '{header}' at column {col_index_to_letter(idx)}"
            )
        field_indexes[field] = idx

    if changed:
        end_col = col_index_to_letter(len(header_values) - 1)
        sheets_call(
            comp_ws.update,
            [header_values],
            f"A1:{end_col}1",
            value_input_option="USER_ENTERED",
        )

    return field_indexes
