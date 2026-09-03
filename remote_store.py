"""
remote_store.py — run the pipeline without any Google credentials.

The scraping itself needs none: it needs the keywords to search for, the
settings to obey, and somewhere to put what it finds. This gets all three from
the Apps Script Web App, authenticated with nothing but the project's password.

    SETTINGS_WEB_APP_URL=https://script.google.com/macros/s/…/exec \
    PROJECT_PASSWORD=… \
    python main.py --config config.yaml

Why it exists. The alternative is handing out the service-account key, and that
key can read and write every spreadsheet it has ever been shared with — there
is no way to scope it to one project. So it cannot leave the owner, and anyone
else running the pipeline needs a path that does not involve it.

It presents the same surface as GoogleSheetsStore, so main.py and
settings_sheet.py use it without knowing the difference. Two things it cannot
do, both deliberate: it never sees the spreadsheet id, and it can only reach
the one project whose password it was given.

Unlike the browser dashboard, this is a program rather than a page, so the
CORS rules that force fire-and-forget writes elsewhere do not apply here — it
POSTs and reads the reply.
"""

import hashlib
import json
import logging
import os
import re
import time

import pandas as pd

import storage

logger = logging.getLogger(__name__)

# Rows per append request. Apps Script stops a single execution at six minutes,
# and a batch this size writes well inside that even on a slow sheet.
APPEND_BATCH = 250

# Each attempt covers a slow Sheets write; the retry is for a script that was
# busy rather than broken.
TIMEOUT_SEC = 180
RETRIES = 3
BACKOFF_SEC = 5.0

LINK_HASH_CHARS = 12

# A column write is one request whatever its length, but Apps Script stops an
# execution at six minutes, so very long columns still go in pieces.
COLUMN_BATCH = 2000

# Each deletion is its own Sheets operation, so these are kept to a size that
# finishes inside one execution.
DELETE_BATCH = 200


def link_hash(url: str, chars: int = LINK_HASH_CHARS) -> str:
    """Short digest of a job URL.

    Kept in step with _linkHash in apps-script/Settings.gs. Already-seen links
    arrive as hashes rather than URLs: it is all that is needed to skip a
    duplicate, and it means a machine running the pipeline is never handed the
    list of jobs already collected.
    """
    return hashlib.sha256(str(url or "").strip().encode()).hexdigest()[:chars]


JSONP_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*\((.*)\);?$", re.DOTALL)


def unwrap_jsonp(text: str) -> str:
    """The JSON inside ``callback({...});``, or *text* unchanged.

    The Web App serves plain JSON to a program and JSONP only when a callback
    is asked for — but a deployment made before it learned that wraps every
    reply, and this has to keep working against one. Sending no callback and
    still unwrapping is what makes the local run work today, without waiting on
    a redeploy.
    """
    stripped = (text or "").strip()
    match = JSONP_RE.match(stripped)
    return match.group(1) if match else stripped


def _setting_from_rows(rows: list, key: str, fallback: str) -> str:
    """One setting's value out of the raw Settings rows."""
    if not rows:
        return fallback
    header = [str(h).strip() for h in rows[0]]
    if "Setting" not in header or "Value" not in header:
        return fallback
    key_at, value_at = header.index("Setting"), header.index("Value")
    for row in rows[1:]:
        if key_at < len(row) and str(row[key_at]).strip() == key:
            value = str(row[value_at]).strip() if value_at < len(row) else ""
            if value:
                return value
    return fallback


class RemoteStoreError(RuntimeError):
    """The Web App could not be reached, or refused."""


class RemoteSheetsStore:
    """A GoogleSheetsStore work-alike backed by the Apps Script Web App.

    Method for method the same surface, argument order included, because the
    pipeline is handed one or the other and must not be able to tell which.
    tests/test_store_parity_unit.py holds that true.
    """

    def __init__(self, exec_url: str, password: str, config: dict = None) -> None:
        self.exec_url = (exec_url or "").strip()
        self.password = password or ""
        self.config = config or {}
        self._inputs = None          # fetched once; a run wants a stable view

    # ── Transport ─────────────────────────────────────────────────────────────

    def _request(self, method: str, **kwargs):
        import requests

        if not self.exec_url:
            raise RemoteStoreError("SETTINGS_WEB_APP_URL is not set")
        if not self.password:
            raise RemoteStoreError("PROJECT_PASSWORD is not set")

        last = None
        for attempt in range(RETRIES):
            try:
                if method == "GET":
                    response = requests.get(self.exec_url, timeout=TIMEOUT_SEC,
                                            params=kwargs.get("params"))
                else:
                    response = requests.post(
                        self.exec_url, timeout=TIMEOUT_SEC,
                        # text/plain keeps it a "simple" request, which is what
                        # the Web App is set up to accept.
                        headers={"Content-Type": "text/plain;charset=utf-8"},
                        data=kwargs.get("data"))
                response.raise_for_status()
                break
            except Exception as exc:                    # network, 5xx, timeout
                last = exc
                if attempt == RETRIES - 1:
                    raise RemoteStoreError(
                        f"could not reach the Settings service after {RETRIES} "
                        f"attempts: {exc}") from exc
                wait = BACKOFF_SEC * (2 ** attempt)
                logger.warning(f"Settings service unreachable (attempt "
                               f"{attempt + 1}/{RETRIES}), retrying in {wait:.0f}s")
                time.sleep(wait)

        text = response.text.strip()
        if text.startswith("<"):
            # Google answers with an HTML page when the script itself refused —
            # most often the deployment cannot open the spreadsheet.
            raise RemoteStoreError(
                "the Settings service returned a web page instead of data, "
                "which usually means the deployment cannot open this project's "
                "spreadsheet")
        try:
            payload = json.loads(unwrap_jsonp(text))
        except ValueError as exc:
            raise RemoteStoreError(f"unreadable reply: {text[:200]}") from exc

        if not payload.get("ok"):
            raise RemoteStoreError(payload.get("error") or "the service refused the request")
        return payload

    def _fetch_inputs(self) -> dict:
        if self._inputs is not None:
            return self._inputs
        logger.info("Fetching this project's inputs from the Settings service")
        payload = self._request("GET", params={
            "action": "inputs", "password": self.password})
        self._inputs = payload
        logger.info(
            f"Project '{payload.get('project')}': "
            f"{len(payload.get('keywords') or [])} keywords, "
            f"{len(payload.get('settingsRows') or []) - 1} settings, "
            f"{len(payload.get('existingLinkHashes') or [])} jobs already collected")
        return payload

    # ── The GoogleSheetsStore surface ─────────────────────────────────────────

    def is_enabled(self) -> bool:
        return bool(self.exec_url and self.password)

    def load_all_rows(self, worksheet_name: str = None) -> list:
        """Raw rows of a tab, header included.

        Settings comes out of the inputs already fetched, since the scrape needs
        it anyway and it saves a round trip. Any other tab is fetched on demand:
        the validator, the enricher, the mismatch and classifier passes and the
        career-page pass all read whole tabs, and refusing them was what kept
        those stages tied to the service-account key.
        """
        if worksheet_name in (None, "Settings"):
            return [list(row) for row in self._fetch_inputs().get("settingsRows") or []]
        payload = self._request("GET", params={
            "action": "rows", "password": self.password,
            "worksheet": worksheet_name})
        return [list(row) for row in payload.get("rows") or []]

    def load_column_values(self, column_header: str, worksheet_name: str) -> list:
        """Non-empty values under a header.

        Keywords are already in hand from the inputs; anything else is read off
        the tab, matching what the Sheets-API store returns for the same call.
        """
        if column_header == "Search Term" or worksheet_name == "Keywords":
            return list(self._fetch_inputs().get("keywords") or [])
        rows = self.load_all_rows(worksheet_name)
        if not rows:
            return []
        header = [str(h).strip() for h in rows[0]]
        if column_header not in header:
            return []
        at = header.index(column_header)
        return [str(row[at]).strip() for row in rows[1:]
                if at < len(row) and str(row[at]).strip()]

    def load_existing(self) -> pd.DataFrame:
        """Existing rows, as far as deduplication needs them.

        Only the Job Link hashes come across, so this is a frame of hashes
        rather than of jobs. deduplicate() keys on Job Link, so that is
        sufficient — and the append call checks again against the live sheet,
        which is the only check that can see rows another machine added while
        this run was going.
        """
        inputs = self._fetch_inputs()
        hashes = inputs.get("existingLinkHashes") or []
        if not hashes:
            return pd.DataFrame(columns=storage.OUTPUT_COLUMNS)
        frame = pd.DataFrame({"Job Link": hashes})
        for column in storage.OUTPUT_COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        return frame[storage.OUTPUT_COLUMNS]

    def load_company_linkedin_map(
        self,
        worksheet_name: str = "Company",
        company_col: str = "Company",
        linkedin_col: str = "Linkedin-Url",
    ) -> dict:
        """{lowercased company name -> LinkedIn URL}, from the inputs.

        The three arguments are the same ones GoogleSheetsStore takes and are
        accepted for that reason — a caller passing them positionally must land
        in the same places. They are not used: which tab and columns to read is
        settled on the far side, from this project's own Settings, so what
        arrives is the finished map. Asking for a different tab than the project
        is configured with would be a caller bug, and it says so rather than
        quietly answering about the configured one.
        """
        inputs = self._fetch_inputs()
        configured = _setting_from_rows(
            inputs.get("settingsRows"),
            "google_sheets.company_sheet.worksheet", "Company")
        if worksheet_name and worksheet_name != configured:
            raise RemoteStoreError(
                f"the company map comes from '{configured}', which is what this "
                f"project's Settings name; '{worksheet_name}' cannot be read "
                "through the Settings service")
        return dict(inputs.get("companyLinkedIn") or {})

    def append_rows(self, df: pd.DataFrame, company_linkedin: dict = None) -> None:
        """Send rows to the Web App, which writes them as the sheet's owner."""
        if df is None or df.empty:
            logger.info("Nothing new to append")
            return

        inputs = self._fetch_inputs()
        chars = int(inputs.get("linkHashChars") or LINK_HASH_CHARS)
        already = set(inputs.get("existingLinkHashes") or [])

        records = df.fillna("").astype(str).to_dict("records")
        fresh = [r for r in records
                 if not (r.get("Job Link") and link_hash(r["Job Link"], chars) in already)]
        if len(fresh) != len(records):
            logger.info(f"{len(records) - len(fresh)} of {len(records)} were already "
                        "in the sheet")
        if not fresh:
            return

        added = duplicates = 0
        for start in range(0, len(fresh), APPEND_BATCH):
            batch = fresh[start:start + APPEND_BATCH]
            payload = self._request("POST", data=json.dumps({
                "action": "appendJobs",
                "password": self.password,
                "worksheet": inputs.get("jobsWorksheet"),
                "rows": batch,
            }))
            added += int(payload.get("added") or 0)
            duplicates += int(payload.get("duplicates") or 0)
            logger.info(f"Appended {added}/{len(fresh)} rows"
                        f"{f' ({duplicates} were duplicates)' if duplicates else ''}")

        logger.info(f"Added {added} rows to '{inputs.get('jobsWorksheet')}'"
                    f"{f'; {duplicates} skipped as duplicates' if duplicates else ''}")


    # ── Writing back ─────────────────────────────────────────────────────────
    #
    # Same signatures as GoogleSheetsStore, down to the argument order, because
    # the callers are handed one or the other and must not be able to tell.

    def ensure_column(self, header_name: str, worksheet_name: str = None) -> int:
        """1-based position of a header, adding the column if it is missing."""
        payload = self._request("POST", data=json.dumps({
            "action": "ensureColumn", "password": self.password,
            "worksheet": worksheet_name or "", "header": header_name}))
        return int(payload.get("position") or 0)

    def write_column_values(self, col: int, values: list,
                            worksheet_name: str = None,
                            start_row: int = 2) -> None:
        """Write a whole column, one request per batch.

        The batching is not an optimisation. Sheets allows 60 writes a minute
        per user, so a cell at a time starts failing a few hundred rows in — and
        the failures were logged and dropped under a summary that read like
        success.
        """
        if not values:
            return
        flat = [(v[0] if isinstance(v, (list, tuple)) else v) for v in values]
        for start in range(0, len(flat), COLUMN_BATCH):
            chunk = flat[start:start + COLUMN_BATCH]
            self._request("POST", data=json.dumps({
                "action": "writeColumn", "password": self.password,
                "worksheet": worksheet_name or "", "col": int(col),
                "startRow": int(start_row) + start,
                "values": ["" if v is None else str(v) for v in chunk]}))
        logger.info(f"Wrote {len(flat)} cells to column {col}")

    def update_cell(self, row: int, col: int, value: str,
                    worksheet_name: str = None) -> None:
        self.write_column_values(col, [[value]], worksheet_name, start_row=row)

    def delete_rows(self, worksheet_name: str, row_numbers: list) -> int:
        """Delete rows by 1-based sheet row number. Returns how many went."""
        rows = sorted({int(n) for n in row_numbers if int(n) >= 2}, reverse=True)
        if not rows:
            return 0
        deleted = 0
        for start in range(0, len(rows), DELETE_BATCH):
            payload = self._request("POST", data=json.dumps({
                "action": "deleteRows", "password": self.password,
                "worksheet": worksheet_name or "",
                "rows": rows[start:start + DELETE_BATCH]}))
            deleted += int(payload.get("deleted") or 0)
        return deleted

    def replace_tab(self, worksheet_name: str, rows: list,
                    freeze_header: bool = True) -> None:
        """Replace a tab's whole contents. Used to rebuild a generated tab."""
        table = [[("" if c is None else str(c)) for c in (row or [])]
                 for row in rows or []]
        if not table:
            raise RemoteStoreError("refusing to replace a tab with nothing")
        self._request("POST", data=json.dumps({
            "action": "replaceTab", "password": self.password,
            "worksheet": worksheet_name or "", "rows": table,
            "freezeHeader": bool(freeze_header)}))
        logger.info(f"Replaced '{worksheet_name}' with {len(table)} rows")

    # Colour is decoration, and the Web App has no action for it. Skipping it
    # loudly beats either crashing a run over formatting or pretending it
    # happened — the data is identical either way.
    def batch_format_cells(self, cell_colors: list, worksheet_name: str = None) -> None:
        if cell_colors:
            logger.info(f"Skipping colour for {len(cell_colors)} cells: "
                        "formatting needs the Sheets API, the data does not")

    def batch_format_rows(self, row_colors: list, num_cols: int,
                          worksheet_name: str = None) -> None:
        wanted = [pair for pair in row_colors if pair[1] is not None]
        if wanted:
            logger.info(f"Skipping colour for {len(wanted)} rows: "
                        "formatting needs the Sheets API, the data does not")

    def open_worksheet(self, name: str):
        """Not possible remotely, and it says which call to use instead.

        A gspread worksheet is a live API handle; there is nothing to hand back.
        Every use of it in the pipeline has a named replacement above, so this
        being reached is a caller that has not been ported rather than a
        limitation to work around.
        """
        raise RemoteStoreError(
            f"open_worksheet('{name}') needs Google credentials. Use "
            "load_all_rows, write_column_values, ensure_column or delete_rows, "
            "which work either way.")


# ── Choosing a store ─────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True when the environment asks for the credential-free path."""
    return bool(os.environ.get("SETTINGS_WEB_APP_URL")
                and os.environ.get("PROJECT_PASSWORD"))


def store_for(config: dict):
    """The right store for how this machine is set up.

    With SETTINGS_WEB_APP_URL and PROJECT_PASSWORD present, the pipeline talks
    to the Web App and needs no Google credentials. Otherwise it uses the
    service-account key as it always has, so nothing about running this on the
    owner's own machine changes.
    """
    if is_configured():
        logger.info("Using the Settings service (no Google credentials needed)")
        return RemoteSheetsStore(
            exec_url=os.environ["SETTINGS_WEB_APP_URL"],
            password=os.environ["PROJECT_PASSWORD"],
            config=config)

    from google_sheets_store import GoogleSheetsStore
    return GoogleSheetsStore(config.get("google_sheets", {}))
