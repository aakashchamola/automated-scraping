import logging

import pandas as pd

import storage

logger = logging.getLogger(__name__)


class GoogleSheetsStore:
    """Simple Google Sheets sink/source for job rows."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self._worksheet = None

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _get_worksheet(self):
        if self._worksheet is not None:
            return self._worksheet

        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheets dependencies missing. Install requirements.txt first."
            ) from exc

        creds_file = self.config.get("credentials_file", "")
        spreadsheet_id = self.config.get("spreadsheet_id", "")
        worksheet_name = self.config.get("worksheet", "Jobs")

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
            spreadsheet = client.open_by_key(spreadsheet_id)
        except PermissionError as exc:
            svc_email = getattr(creds, "service_account_email", "<service-account-email>")
            raise RuntimeError(
                "Google Sheets access denied (403). Share the target sheet with this "
                f"service account email as Editor: {svc_email}"
            ) from exc

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=20)

        self._worksheet = worksheet
        self._ensure_header()
        return worksheet

    def _ensure_header(self) -> None:
        worksheet = self._get_worksheet()
        values = worksheet.get_all_values()
        if not values:
            worksheet.append_row(storage.OUTPUT_COLUMNS, value_input_option="RAW")
            return

        header = values[0]
        if header != storage.OUTPUT_COLUMNS:
            worksheet.update("A1:F1", [storage.OUTPUT_COLUMNS])

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

    def append_rows(self, df: pd.DataFrame) -> None:
        if df.empty:
            logger.info("No new rows to append to Google Sheets")
            return

        worksheet = self._get_worksheet()
        prepared = storage.prepare_df(df)
        rows = prepared.values.tolist()
        worksheet.append_rows(rows, value_input_option="RAW")
        logger.info(f"Appended {len(rows)} new rows to Google Sheets")
