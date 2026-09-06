"""
projects_registry.py — the control spreadsheet that lists every project.

One spreadsheet is the registry; every project it names has its own spreadsheet
holding that project's Jobs, Companies, Keywords and Settings tabs. Nothing about
a project lives in this repository, so adding one needs no commit, no secret and
no redeploy — you create a sheet, share it with the service account, and add a
row here.

    Projects tab
      id  name  spreadsheet_id  status  data_key  pw_salt  pw_hash  created_at  notes

Two secrets per project, and they are deliberately different things:

  data_key   encrypts the published dashboard files. Generated once and never
             changed — rotating it would strand every file already published.
             The pipeline reads it here (as the service account) to encrypt;
             the browser is handed it by the Apps Script only after the password
             checks out.

  pw_hash    what the operator types, salted and iterated. This is the one that
             can be changed freely, precisely because it is not the data key.

On the hash: it is iterated SHA-256, not PBKDF2, because Apps Script has no
PBKDF2 and the same hash has to be computable there. That is weaker against
offline cracking, and it is an acceptable trade only because the hash lives in a
private spreadsheet — anyone who can read it can already read every project's
data directly, so the hash guards nothing they do not already hold.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WORKSHEET = "Projects"
HEADER = ["id", "name", "spreadsheet_id", "status", "data_key",
          "pw_salt", "pw_hash", "created_at", "notes"]

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

# Kept in step with hashPassword() in apps-script/Settings.gs. Changing it
# invalidates every stored hash, so it is a constant, not a setting.
HASH_ROUNDS = 1000

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ── Passwords ─────────────────────────────────────────────────────────────────

def new_salt() -> str:
    return secrets.token_hex(16)


def new_data_key() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str, salt: str, rounds: int = HASH_ROUNDS) -> str:
    """Salted, iterated SHA-256 over hex strings.

    Hex at every step, rather than raw bytes, so Apps Script can reproduce it
    with Utilities.computeDigest without any byte-signedness games.
    """
    digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    for _ in range(rounds - 1):
        digest = hashlib.sha256(digest.encode()).hexdigest()
    return digest


def verify_password(project: dict, password: str) -> bool:
    """True when ``password`` matches this project's stored hash."""
    stored = (project or {}).get("pw_hash") or ""
    salt = (project or {}).get("pw_salt") or ""
    if not stored or not salt:
        return False
    return hmac.compare_digest(stored, hash_password(password, salt))


def validate_id(project_id: str) -> str:
    """Normalise and check a project id, or raise ValueError."""
    candidate = (project_id or "").strip().lower()
    if not _ID_RE.match(candidate):
        raise ValueError(
            f"invalid project id {project_id!r}: use 1-32 characters, "
            "lowercase letters, digits, '-' or '_', starting with a letter or digit")
    return candidate


def slugify(name: str) -> str:
    """Best-effort project id from a human name ('Biotech Jobs' -> 'biotech-jobs')."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (slug or "project")[:32]


# ── Registry ──────────────────────────────────────────────────────────────────

class ProjectRegistry:
    """Read/write access to the Projects tab of the control spreadsheet."""

    def __init__(self, spreadsheet_id: str, credentials_file: str,
                 worksheet: str = WORKSHEET) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_file = credentials_file
        self.worksheet_name = worksheet
        self._worksheet = None

    # ── Connection ────────────────────────────────────────────────────────────

    def _open(self):
        if self._worksheet is not None:
            return self._worksheet

        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheets dependencies missing. Install requirements.txt first."
            ) from exc

        if not self.spreadsheet_id or not self.credentials_file:
            raise RuntimeError(
                "Missing control-sheet config. Set control.spreadsheet_id and "
                "google_sheets.credentials_file.")

        creds = Credentials.from_service_account_file(
            self.credentials_file, scopes=SCOPES)
        client = gspread.authorize(creds)
        try:
            spreadsheet = client.open_by_key(self.spreadsheet_id)
        except PermissionError as exc:
            email = getattr(creds, "service_account_email", "<service-account-email>")
            raise RuntimeError(
                "Control spreadsheet access denied (403). Share it with this "
                f"service account as Editor: {email}") from exc

        try:
            worksheet = spreadsheet.worksheet(self.worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=self.worksheet_name, rows=200, cols=len(HEADER))
            worksheet.update("A1", [HEADER])
            worksheet.freeze(rows=1)

        self._worksheet = worksheet
        return worksheet

    def ensure_tab(self) -> None:
        """Create the Projects tab and its header if either is missing."""
        worksheet = self._open()
        header = worksheet.row_values(1)
        if header[:len(HEADER)] != HEADER:
            worksheet.update("A1", [HEADER])
            worksheet.freeze(rows=1)
            logger.info("Wrote the Projects header")

    # ── Reads ─────────────────────────────────────────────────────────────────

    def _rows(self) -> list:
        """Every project row as a dict, with its 1-based sheet row number."""
        values = self._open().get_all_values()
        if not values:
            return []
        header = values[0]
        rows = []
        for offset, raw in enumerate(values[1:], start=2):
            record = {key: (raw[i] if i < len(raw) else "")
                      for i, key in enumerate(header)}
            if not (record.get("id") or "").strip():
                continue
            record["_row"] = offset
            rows.append(record)
        return rows

    def list(self, include_archived: bool = False) -> list:
        rows = self._rows()
        if include_archived:
            return rows
        return [r for r in rows if (r.get("status") or "").lower() != STATUS_ARCHIVED]

    def get(self, project_id: str) -> dict:
        wanted = (project_id or "").strip().lower()
        for row in self._rows():
            if (row.get("id") or "").strip().lower() == wanted:
                return row
        return None

    def find_by_password(self, password: str) -> dict:
        """The project this password unlocks, or None.

        Every active project is checked even after a match, so the time taken
        does not reveal how far down the list the answer was.
        """
        found = None
        for row in self.list():
            if verify_password(row, password) and found is None:
                found = row
        return found

    # ── Writes ────────────────────────────────────────────────────────────────

    def create(self, name: str, spreadsheet_id: str, password: str,
               project_id: str = None, notes: str = "",
               data_key: str = None) -> dict:
        """Add a project row. Returns the created record."""
        project_id = validate_id(project_id or slugify(name))
        if self.get(project_id):
            raise ValueError(f"project id {project_id!r} already exists")
        if not (spreadsheet_id or "").strip():
            raise ValueError("spreadsheet_id is required")
        if not password:
            raise ValueError("a password is required")

        salt = new_salt()
        record = {
            "id": project_id,
            "name": (name or project_id).strip(),
            "spreadsheet_id": spreadsheet_id.strip(),
            "status": STATUS_ACTIVE,
            "data_key": data_key or new_data_key(),
            "pw_salt": salt,
            "pw_hash": hash_password(password, salt),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notes": notes or "",
        }

        self.ensure_tab()
        self._open().append_row([record[key] for key in HEADER],
                                value_input_option="RAW")
        logger.info(f"Created project '{project_id}' -> {record['spreadsheet_id']}")
        return record

    def update(self, project_id: str, **fields) -> dict:
        """Change named columns on one project row."""
        row = self.get(project_id)
        if not row:
            raise ValueError(f"no such project: {project_id!r}")

        unknown = set(fields) - set(HEADER)
        if unknown:
            raise ValueError(f"unknown column(s): {', '.join(sorted(unknown))}")

        worksheet = self._open()
        header = worksheet.row_values(1)
        for key, value in fields.items():
            column = header.index(key) + 1
            worksheet.update_cell(row["_row"], column, value)
            row[key] = value
        return row

    def set_password(self, project_id: str, password: str) -> dict:
        if not password:
            raise ValueError("a password is required")
        salt = new_salt()
        return self.update(project_id, pw_salt=salt,
                           pw_hash=hash_password(password, salt))

    def archive(self, project_id: str) -> dict:
        return self.update(project_id, status=STATUS_ARCHIVED)


# ── Wiring into a config ──────────────────────────────────────────────────────

def registry_from_config(config: dict) -> ProjectRegistry:
    """Build a registry from the ``control`` block, honouring the env override."""
    control = config.get("control") or {}
    sheets = config.get("google_sheets") or {}
    spreadsheet_id = (os.environ.get("CONTROL_SPREADSHEET_ID")
                      or control.get("spreadsheet_id") or "")
    return ProjectRegistry(
        spreadsheet_id=spreadsheet_id,
        credentials_file=control.get("credentials_file")
                         or sheets.get("credentials_file", ""),
        worksheet=control.get("worksheet", WORKSHEET))


def is_enabled(config: dict) -> bool:
    """True when a control spreadsheet is configured."""
    control = config.get("control") or {}
    if not control.get("enabled", True):
        return False
    return bool(os.environ.get("CONTROL_SPREADSHEET_ID")
                or control.get("spreadsheet_id"))


def apply_project(config: dict, project: dict) -> dict:
    """Point a loaded config at one project's spreadsheet.

    Only the spreadsheet id moves. Everything else about how that project runs
    lives in its own Settings tab, which is overlaid separately — so this stays
    a one-line switch no matter how many settings exist.
    """
    config.setdefault("google_sheets", {})["spreadsheet_id"] = project["spreadsheet_id"]
    config["active_project"] = {"id": project["id"], "name": project.get("name", "")}
    return config


def resolve(config: dict, project_id: str = None) -> dict:
    """Select a project and point ``config`` at it. Returns the project, or None.

    With no control sheet configured, or no project asked for and none in the
    registry, the config is left exactly as it is — which is what keeps a
    single-sheet setup working unchanged.

    ── WHY A PASSWORD SKIPS ALL OF THIS ──────────────────────────────────────
    On a machine holding only a project password there is nothing to resolve:
    the password already chose the project, and the Web App will only ever act
    on that one. The control sheet is not merely unavailable there, it is
    deliberately out of reach — it lists every project and holds every data key
    and password hash, which is precisely what a password must not reach.

    Without this, every entry point died on the first line of its main(): it
    called this before choosing a store, and this opened the control sheet with
    the service-account key. That went unnoticed for exactly the reason it is
    easy to miss — the machine it was developed on has the key, so the registry
    opened fine and the run went on to use the remote store as intended. On a
    fresh install there is no key, and all ten run modes stopped with a
    FileNotFoundError before doing any work.
    """
    import remote_store          # local: keeps this module importable alone

    if remote_store.is_configured():
        if project_id:
            raise RuntimeError(
                "--project cannot be used with PROJECT_PASSWORD: the password "
                "already selects the project, and this machine cannot see the "
                "list of others.")
        return None

    if not is_enabled(config):
        if project_id:
            raise RuntimeError(
                "--project was given but no control spreadsheet is configured. "
                "Set control.spreadsheet_id in config.yaml.")
        return None

    registry = registry_from_config(config)
    if project_id:
        project = registry.get(project_id)
        if not project:
            known = ", ".join(p["id"] for p in registry.list()) or "none"
            raise RuntimeError(
                f"no such project: {project_id!r}. Known projects: {known}")
    else:
        active = registry.list()
        if not active:
            return None
        project = active[0]

    apply_project(config, project)
    logger.info(f"Project '{project['id']}' -> spreadsheet {project['spreadsheet_id']}")
    return project


def lookup(project_id: str, config_path: str = "config.yaml") -> dict:
    """Find a project via the control block of a base config file.

    For tools that carry their own config format and so have no ``control``
    block of their own to read.
    """
    from config_loader import load_config
    return resolve(load_config(config_path), project_id)


def add_project_argument(parser) -> None:
    """Add the shared --project flag to an argparse parser."""
    parser.add_argument(
        "--project", default=os.environ.get("PROJECT_ID") or None,
        help="project id from the control spreadsheet (default: the first active one)")
