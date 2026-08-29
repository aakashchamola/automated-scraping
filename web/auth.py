"""
web/auth.py — Login for the control panel.

The Run tab executes commands on the host and the Google key in secrets/ has
write access to the whole spreadsheet, so this is a real gate, not decoration.

Credentials never live in the repository — it is public. They are read, in
order, from:

  1. DASHBOARD_USER + DASHBOARD_PASSWORD_HASH in the environment
  2. .dashboard-auth.json beside the project (gitignored)

Both hold a *hash*, never the password itself. Create or change them with:

    python -m web.setup_auth
"""

import functools
import hmac
import json
import os
import secrets
import time

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FILE = os.path.join(PROJECT_ROOT, ".dashboard-auth.json")

SESSION_DAYS = 10         # a signed-in browser is not asked again for this long
MAX_ATTEMPTS = 8          # per source address, per window
ATTEMPT_WINDOW = 300      # seconds

bp = Blueprint("auth", __name__)

# Failed attempts per remote address. In-memory is the right scope: this server
# is single-process and localhost-only, and a restart clearing it is harmless.
_attempts = {}


# ── Credential source ────────────────────────────────────────────────────────

def load_credentials() -> dict:
    """{"username", "password_hash", "secret_key"} or {} when unconfigured."""
    user = os.environ.get("DASHBOARD_USER", "").strip()
    hashed = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
    if user and hashed:
        return {"username": user, "password_hash": hashed,
                "secret_key": os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)}

    try:
        with open(AUTH_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    if not data.get("username") or not data.get("password_hash"):
        return {}
    # Sessions must survive a restart, so the key is stored rather than random.
    data.setdefault("secret_key", secrets.token_hex(32))
    return data


def save_credentials(username: str, password: str) -> str:
    """Write a hashed credential file, readable only by its owner."""
    payload = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "secret_key": secrets.token_hex(32),
    }
    with open(AUTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(AUTH_FILE, 0o600)
    return AUTH_FILE


# ── Gate ─────────────────────────────────────────────────────────────────────

def _throttled(addr: str) -> int:
    """Seconds the caller must wait, or 0. Slows password guessing without
    locking anyone out permanently."""
    now = time.time()
    hits = [t for t in _attempts.get(addr, []) if now - t < ATTEMPT_WINDOW]
    _attempts[addr] = hits
    if len(hits) < MAX_ATTEMPTS:
        return 0
    return int(ATTEMPT_WINDOW - (now - hits[0])) + 1


def _record_failure(addr: str) -> None:
    _attempts.setdefault(addr, []).append(time.time())


def is_logged_in() -> bool:
    return bool(session.get("user"))


def login_required(view):
    """Protect a view. API routes get 401 JSON; pages get the login screen."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if is_logged_in():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "not signed in", "login_required": True}), 401
        return redirect(url_for("auth.login", next=request.path))
    return wrapped


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
def login():
    creds = current_app.config["DASHBOARD_CREDENTIALS"]
    target = request.args.get("next", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"                                  # never redirect off-site

    if request.method == "GET":
        if is_logged_in():
            return redirect(target)
        return render_template("login.html", error=None, next=target)

    addr = request.remote_addr or "?"
    wait = _throttled(addr)
    if wait:
        return render_template(
            "login.html", next=target,
            error=f"Too many attempts. Try again in {wait} seconds."), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    # Compare both halves every time so a wrong username and a wrong password
    # take the same work and cannot be told apart by timing.
    user_ok = hmac.compare_digest(username, creds["username"])
    pass_ok = check_password_hash(creds["password_hash"], password)
    if user_ok and pass_ok:
        session.clear()
        session["user"] = creds["username"]
        session.permanent = True
        _attempts.pop(addr, None)
        return redirect(target)

    _record_failure(addr)
    return render_template("login.html", next=target,
                           error="Incorrect username or password."), 401


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/api/whoami")
def whoami():
    return jsonify({"user": session.get("user"), "signed_in": is_logged_in()})
