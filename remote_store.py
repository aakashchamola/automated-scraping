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
import logging
import os
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


def link_hash(url: str, chars: int = LINK_HASH_CHARS) -> str:
    """Short digest of a job URL.

    Kept in step with _linkHash in apps-script/Settings.gs. Already-seen links
    arrive as hashes rather than URLs: it is all that is needed to skip a
    duplicate, and it means a machine running the pipeline is never handed the
    list of jobs already collected.
    """
    return hashlib.sha256(str(url or "").strip().encode()).hexdigest()[:chars]


class RemoteStoreError(RuntimeError):
    """The Web App could not be reached, or refused."""


class RemoteSheetsStore:
    """A GoogleSheetsStore work-alike backed by the Apps Script Web App."""

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
            import json
            payload = json.loads(text)
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
            "action": "inputs", "password": self.password, "callback": "x"})
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
        """Raw rows of a tab. Only Settings is available remotely.

        The others are either handed over in a shape the pipeline actually uses
        (keywords, the company map) or deliberately withheld (the jobs already
        collected). Asking for one of those is a bug rather than a fallback, so
        it says so.
        """
        inputs = self._fetch_inputs()
        if worksheet_name in (None, "Settings"):
            return [list(row) for row in inputs.get("settingsRows") or []]
        raise RemoteStoreError(
            f"'{worksheet_name}' cannot be read through the Settings service; "
            "only the Settings tab is available without Google credentials")

    def load_column_values(self, column_header: str, worksheet_name: str) -> list:
        inputs = self._fetch_inputs()
        if column_header == "Search Term" or worksheet_name == "Keywords":
            return list(inputs.get("keywords") or [])
        raise RemoteStoreError(
            f"column '{column_header}' of '{worksheet_name}' is not available "
            "through the Settings service")

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

    def load_company_linkedin_map(self, *args, **kwargs) -> dict:
        return dict(self._fetch_inputs().get("companyLinkedIn") or {})

    def append_rows(self, df: pd.DataFrame, company_linkedin: dict = None) -> None:
        """Send rows to the Web App, which writes them as the sheet's owner."""
        import json

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
