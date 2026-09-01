import logging
import time

import pandas as pd

import storage

logger = logging.getLogger(__name__)

# Retries and backoff for sheets_call(). Four attempts at 8s doubling covers
# 8+16+32 = 56s of waiting, which clears a "write requests per minute" quota
# window with room to spare.
RETRIES = 4
BACKOFF_SEC = 8.0

# Worth retrying: a per-minute quota that will have refilled, and the transient
# 5xx family. Anything else — 400, 403, 404 — means the request itself is wrong
# and retrying only delays the error.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def sheets_call(fn, *args, retries: int = RETRIES,
                backoff: float = BACKOFF_SEC, **kwargs):
    """Call a gspread method, retrying quota and transient errors.

    Every write in this module goes through here, so callers do not have to
    remember to ask for it. That matters most for the quota case: Sheets allows
    sixty write requests a minute, and a job that writes steadily will meet a
    429 sooner or later however well it batches. Failing the whole run for a
    limit that refills in under a minute is a poor trade.

    Batching and retrying solve different halves of the same problem — batching
    makes the limit unlikely to be reached, retrying survives it when something
    else is writing the same sheet at the same time.
    """
    import requests.exceptions

    last_exc = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            reason = f"network error: {exc}"
        except Exception as exc:
            # Two clients reach Sheets here — gspread for values, and
            # googleapiclient for the formatting batchUpdate — and they raise
            # different exception types with the status in different places.
            # Catching only one of them silently disables the retry for half
            # the writes in this module.
            status = _http_status(exc)
            if status not in RETRYABLE_STATUS:
                raise
            last_exc = exc
            reason = f"API error {status}"

        wait = backoff * (2 ** attempt)
        if attempt >= retries - 1:
            break              # do not wait for a retry that will never happen
        logger.warning(f"Sheets {reason} (attempt {attempt + 1}/{retries}), "
                       f"retrying in {wait:.0f}s")
        time.sleep(wait)
    raise last_exc


def _http_status(exc) -> int:
    """The HTTP status behind a Sheets exception, or 0 if it is not one.

    gspread.exceptions.APIError carries a requests Response on .response;
    googleapiclient.errors.HttpError carries an httplib2 response on .resp,
    where the status lives under .status. Read both without importing either,
    so this stays honest whichever client raised.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    return 0

# Columns that mean the same thing across different sheet layouts.
_ALIAS_GROUPS = [
    {"Platform", "Application Platform"},
]


def _align_record_to_header(record: dict, header: list) -> list:
    """Build a row list ordered to match ``header``.

    For each sheet column, use the same-named field in ``record``; if absent,
    try any aliased name (e.g. "Application Platform" <- "Platform"). Unknown
    columns become "".
    """
    row = []
    for col in header:
        value = record.get(col, "")
        if not value:
            for group in _ALIAS_GROUPS:
                if col in group:
                    for alt in group:
                        if record.get(alt):
                            value = record[alt]
                            break
        row.append(value if value is not None else "")
    return row


def _hyperlink_formula(url: str, label: str) -> str:
    """Build a Google Sheets HYPERLINK formula (needs valueInputOption USER_ENTERED)."""
    safe_url = (url or "").replace('"', "%22")
    safe_label = str(label or "").replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_label}")'


def _build_append_rows(records: list, header: list, company_linkedin: dict = None) -> list:
    """Align records to header; turn the Company cell into a LinkedIn hyperlink.

    ``company_linkedin`` maps lowercased company name -> LinkedIn URL. When a
    row's company is found there, its Company cell becomes a HYPERLINK formula.
    """
    company_idx = header.index("Company") if "Company" in header else None
    rows = []
    for rec in records:
        row = _align_record_to_header(rec, header)
        if company_linkedin and company_idx is not None:
            name = (row[company_idx] or "").strip()
            url = company_linkedin.get(name.lower())
            if name and url:
                row[company_idx] = _hyperlink_formula(url, name)
        rows.append(row)
    return rows


class GoogleSheetsStore:
    """Google Sheets sink/source for job rows.

    The default worksheet (config "worksheet") is used by the keyword
    pipeline and gets a job-schema header enforced. Other worksheets can be
    opened by name via ``open_worksheet`` WITHOUT header enforcement, so the
    Companies sheet (different columns) is never corrupted.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self._worksheet = None          # default (job) worksheet, header-enforced
        self._spreadsheet = None        # cached spreadsheet handle
        self._worksheets: dict = {}      # name -> worksheet (no header enforcement)

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    # ── Connection ────────────────────────────────────────────────────────────

    def _get_spreadsheet(self):
        if self._spreadsheet is not None:
            return self._spreadsheet

        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheets dependencies missing. Install requirements.txt first."
            ) from exc

        creds_file = self.config.get("credentials_file", "")
        spreadsheet_id = self.config.get("spreadsheet_id", "")

        if not creds_file or not spreadsheet_id:
            raise RuntimeError(
                "Missing Google Sheets config. Set credentials_file and spreadsheet_id."
            )

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        client = gspread.authorize(creds)
        try:
            self._spreadsheet = client.open_by_key(spreadsheet_id)
        except PermissionError as exc:
            svc_email = getattr(creds, "service_account_email", "<service-account-email>")
            raise RuntimeError(
                "Google Sheets access denied (403). Share the target sheet with this "
                f"service account email as Editor: {svc_email}"
            ) from exc
        return self._spreadsheet

    def _get_worksheet(self):
        """Default job worksheet (config "worksheet"), with job header enforced."""
        if self._worksheet is not None:
            return self._worksheet

        import gspread

        worksheet_name = self.config.get("jobs_worksheet", "Jobs")
        spreadsheet = self._get_spreadsheet()
        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=20)

        self._worksheet = worksheet
        self._ensure_header()
        return worksheet

    def open_worksheet(self, name: str):
        """Open (and cache) an arbitrary worksheet by name. No header enforcement.

        Creates the worksheet if it does not exist.
        """
        if name in self._worksheets:
            return self._worksheets[name]

        import gspread

        spreadsheet = self._get_spreadsheet()
        try:
            worksheet = spreadsheet.worksheet(name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=20)

        self._worksheets[name] = worksheet
        return worksheet

    def _ensure_header(self) -> None:
        """Write the default header ONLY when the sheet is empty.

        Never overwrites an existing header — sheets in the wild may use a
        richer schema (e.g. an "Application Platform" / "Sourced By" layout),
        and clobbering row 1 would corrupt column meanings for all data below.
        """
        worksheet = self._worksheet
        values = worksheet.get_all_values()
        if not values or not any(c.strip() for c in values[0]):
            sheets_call(worksheet.update, "A1", [storage.OUTPUT_COLUMNS])

    # ── Generic read/write ──────────────────────────────────────────────────────

    def load_all_rows(self, worksheet_name: str = None) -> list:
        """Return raw rows (list of lists), including the header row."""
        if worksheet_name:
            worksheet = self.open_worksheet(worksheet_name)
        else:
            worksheet = self._get_worksheet()
        return worksheet.get_all_values()

    def load_column_values(self, column_header: str, worksheet_name: str) -> list:
        """Return non-empty, stripped values under ``column_header`` in a worksheet.

        Header is matched by name (so column order doesn't matter). Returns []
        if the header is absent.
        """
        rows = self.load_all_rows(worksheet_name)
        if not rows:
            return []
        header = rows[0]
        if column_header not in header:
            logger.warning(
                f"Column '{column_header}' not found in '{worksheet_name}' header: {header}"
            )
            return []
        idx = header.index(column_header)
        return [
            row[idx].strip()
            for row in rows[1:]
            if idx < len(row) and row[idx].strip()
        ]

    def ensure_column(self, header_name: str, worksheet_name: str = None) -> int:
        """Ensure a column with ``header_name`` exists; return its 1-indexed position.

        Appends the column to the header row if missing.
        """
        worksheet = (
            self.open_worksheet(worksheet_name) if worksheet_name else self._get_worksheet()
        )
        values = worksheet.get_all_values()
        header = values[0] if values else []
        if header_name in header:
            return header.index(header_name) + 1
        new_col = len(header) + 1
        sheets_call(worksheet.update_cell, 1, new_col, header_name)
        return new_col

    def update_cell(
        self, row: int, col: int, value: str, worksheet_name: str = None
    ) -> None:
        """Update a single cell (1-indexed row and col)."""
        worksheet = (
            self.open_worksheet(worksheet_name) if worksheet_name else self._get_worksheet()
        )
        sheets_call(worksheet.update_cell, row, col, value)

    def write_column_values(
        self,
        col: int,
        values: list,
        worksheet_name: str = None,
        start_row: int = 2,
    ) -> None:
        """Write a whole column in one batched update.

        col       : 1-indexed column number.
        values    : list of single-element lists, e.g. [["Company"], ["University"]].
        start_row : 1-indexed first row to write (default 2 = below the header).
        """
        if not values:
            return
        import gspread

        worksheet = (
            self.open_worksheet(worksheet_name) if worksheet_name else self._get_worksheet()
        )
        end_row = start_row + len(values) - 1
        rng = f"{gspread.utils.rowcol_to_a1(start_row, col)}:{gspread.utils.rowcol_to_a1(end_row, col)}"
        sheets_call(worksheet.update, rng, values, value_input_option="RAW")
        logger.info(f"Wrote {len(values)} cells to column {col} ({rng})")

    def batch_format_cells(
        self, cell_colors: list, worksheet_name: str = None
    ) -> None:
        """Color individual cells in one batchUpdate call.

        cell_colors : list of (1-indexed row_num, 1-indexed col_num, bg_color_dict).
        """
        if not cell_colors:
            return

        worksheet = (
            self.open_worksheet(worksheet_name) if worksheet_name else self._get_worksheet()
        )

        try:
            from googleapiclient.discovery import build
            from google.oauth2.service_account import Credentials
        except ImportError:
            logger.warning("googleapiclient not available; skipping cell formatting")
            return

        creds_file = self.config.get("credentials_file", "")
        spreadsheet_id = self.config.get("spreadsheet_id", "")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        sheets_api = build("sheets", "v4", credentials=creds)

        requests_list = []
        for row_num, col_num, bg_color in cell_colors:
            requests_list.append({
                "updateCells": {
                    "range": {
                        "sheetId":          worksheet.id,
                        "startRowIndex":    row_num - 1,
                        "endRowIndex":      row_num,
                        "startColumnIndex": col_num - 1,
                        "endColumnIndex":   col_num,
                    },
                    "rows": [{"values": [{"userEnteredFormat": {"backgroundColor": bg_color}}]}],
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        CHUNK = 200
        for i in range(0, len(requests_list), CHUNK):
            chunk = requests_list[i: i + CHUNK]
            try:
                sheets_call(sheets_api.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": chunk},
                ).execute)
            except Exception as exc:
                logger.warning(f"Failed to format cell batch {i}–{i + len(chunk)}: {exc}")

        logger.info(f"Formatted {len(cell_colors)} individual cells")

    def batch_format_rows(
        self, row_colors: list, num_cols: int, worksheet_name: str = None
    ) -> None:
        """Color entire rows in a single batchUpdate API call.

        row_colors : list of (1-indexed row_num, bg_color_dict or None).
                     Rows with None color are skipped.
        num_cols   : how many columns wide to color (starting from column A).
        """
        to_format = [(r, c) for r, c in row_colors if c is not None]
        if not to_format:
            return

        worksheet = (
            self.open_worksheet(worksheet_name) if worksheet_name else self._get_worksheet()
        )

        try:
            from googleapiclient.discovery import build
            from google.oauth2.service_account import Credentials
        except ImportError:
            logger.warning("googleapiclient not available; skipping row formatting")
            return

        creds_file = self.config.get("credentials_file", "")
        spreadsheet_id = self.config.get("spreadsheet_id", "")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        sheets_api = build("sheets", "v4", credentials=creds)

        requests = []
        for row_num, bg_color in to_format:
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": row_num - 1,
                        "endRowIndex": row_num,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "rows": [{
                        "values": [
                            {"userEnteredFormat": {"backgroundColor": bg_color}}
                            for _ in range(num_cols)
                        ]
                    }],
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        # Send in chunks of 200 to stay well under API size limits
        CHUNK = 200
        for i in range(0, len(requests), CHUNK):
            chunk = requests[i: i + CHUNK]
            try:
                sheets_call(sheets_api.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": chunk},
                ).execute)
            except Exception as exc:
                logger.warning(f"Failed to format row batch {i}–{i + len(chunk)}: {exc}")

        logger.info(f"Applied row colors to {len(to_format)} rows")

    # ── Job-row helpers ───────────────────────────────────────────────────────

    def load_existing(self) -> pd.DataFrame:
        worksheet = self._get_worksheet()
        values = worksheet.get_all_values()

        if len(values) <= 1:
            return pd.DataFrame(columns=storage.OUTPUT_COLUMNS)

        header = values[0]
        rows = values[1:]
        df = pd.DataFrame(rows, columns=header)
        prepared = storage.prepare_df(df)
        logger.info(f"Loaded {len(prepared)} existing records from Google Sheets")
        return prepared

    def append_rows(self, df: pd.DataFrame, company_linkedin: dict = None) -> None:
        if df.empty:
            logger.info("No new rows to append to Google Sheets")
            return

        worksheet = self._get_worksheet()
        prepared = storage.prepare_df(df)

        # Align to the sheet's ACTUAL header so values land in the right columns
        # regardless of column order or alias (Platform vs Application Platform).
        existing = worksheet.get_all_values()
        header = existing[0] if existing and any(c.strip() for c in existing[0]) else storage.OUTPUT_COLUMNS
        rows = _build_append_rows(
            prepared.to_dict("records"), header, company_linkedin
        )
        # USER_ENTERED so the Company HYPERLINK formula renders as a clickable link.
        sheets_call(worksheet.append_rows, rows, value_input_option="USER_ENTERED")
        linked = sum(1 for r in rows if str(r[header.index("Company")]).startswith("=HYPERLINK")) if "Company" in header else 0
        logger.info(
            f"Appended {len(rows)} new rows to Google Sheets "
            f"(aligned to {len(header)} columns, {linked} company links)"
        )

    def load_company_linkedin_map(
        self,
        worksheet_name: str = "Company",
        company_col: str = "Company",
        linkedin_col: str = "Linkedin-Url",
    ) -> dict:
        """Return {lowercased company name -> LinkedIn URL} from a company sheet.

        Used to hyperlink the Company cell of appended job rows.
        """
        rows = self.load_all_rows(worksheet_name)
        if not rows:
            return {}
        header = rows[0]
        if company_col not in header or linkedin_col not in header:
            logger.warning(
                f"Company/LinkedIn columns not found in '{worksheet_name}' header: {header}"
            )
            return {}
        ci, li = header.index(company_col), header.index(linkedin_col)
        out = {}
        for r in rows[1:]:
            if ci >= len(r) or li >= len(r):
                continue
            name = r[ci].strip()
            url = r[li].strip()
            if name and url.startswith("http") and "linkedin.com" in url:
                out[name.lower()] = url
        return out
