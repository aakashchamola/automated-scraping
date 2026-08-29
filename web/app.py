"""
web/app.py — Control panel for the job-scraping automation.

Three things in one page, all against the same config.yaml the CLI uses:

  Data      browse and filter the Google Sheet tabs the automation fills
  Run       launch any tool and watch its log stream live
  Settings  every switch in config.yaml as a labelled control

Start it with:

    python -m web.app            # http://127.0.0.1:5000

It binds to localhost only. The service-account key in secrets/ gives full
read/write on the spreadsheet, so this must not be exposed to a network
without putting authentication in front of it first.
"""

import json
import os
import sys
import webbrowser

from datetime import timedelta

from flask import Flask, Response, jsonify, render_template, request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from web import auth, settings, sheets_data                # noqa: E402
from web.runner import RunManager                          # noqa: E402
from web.tasks import TASKS, TASKS_BY_KEY, build_command   # noqa: E402

app = Flask(__name__)
manager = RunManager()

# Sign-in gate. The Run tab executes commands on this host and secrets/ holds a
# key with write access to the whole spreadsheet, so every route below is
# protected — there is no read-only tier to leave open.
CREDENTIALS = auth.load_credentials()
app.config["DASHBOARD_CREDENTIALS"] = CREDENTIALS
app.secret_key = CREDENTIALS.get("secret_key") or os.urandom(32)
app.permanent_session_lifetime = timedelta(hours=auth.SESSION_HOURS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # a stolen XSS payload cannot read the cookie
    SESSION_COOKIE_SAMESITE="Lax",  # no cross-site form can act as the signed-in user
)
app.register_blueprint(auth.bp)


@app.before_request
def require_sign_in():
    """One gate for everything except the login screen and its stylesheet."""
    if request.endpoint in ("auth.login", "auth.logout", "static"):
        return None
    if auth.is_logged_in():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "not signed in", "login_required": True}), 401
    from flask import redirect, url_for
    return redirect(url_for("auth.login", next=request.path))

# The venv interpreter running this server — never a bare "python", which on
# this machine resolves to a different environment without gspread installed.
PYTHON = sys.executable


@app.route("/")
def index():
    return render_template("index.html")


# ── Data ──────────────────────────────────────────────────────────────────────

@app.route("/api/targets")
def api_targets():
    return jsonify(sheets_data.targets())


@app.route("/api/sheet/<worksheet>")
def api_sheet(worksheet: str):
    force = request.args.get("refresh") == "1"
    try:
        return jsonify(sheets_data.read(worksheet, force=force))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502


# ── Runs ──────────────────────────────────────────────────────────────────────

@app.route("/api/tasks")
def api_tasks():
    return jsonify({"tasks": TASKS})


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(force=True) or {}
    task = TASKS_BY_KEY.get(body.get("task", ""))
    if task is None:
        return jsonify({"error": "unknown task"}), 400

    command = build_command(PYTHON, task, body.get("options") or {})
    run, refusal = manager.start(task["key"], task["label"], command)
    if run is None:
        return jsonify({"error": refusal}), 409
    return jsonify(run.summary())


@app.route("/api/run/<run_id>")
def api_run_status(run_id: str):
    run = manager.get(run_id)
    if run is None:
        return jsonify({"error": "no such run"}), 404
    return jsonify(run.summary())


@app.route("/api/run/<run_id>/stop", methods=["POST"])
def api_run_stop(run_id: str):
    run = manager.get(run_id)
    if run is None:
        return jsonify({"error": "no such run"}), 404
    return jsonify({"stopped": run.stop(), **run.summary()})


@app.route("/api/runs")
def api_runs():
    current = manager.current()
    return jsonify({"current": current.summary() if current else None,
                    "history": manager.history()})


@app.route("/api/run/<run_id>/stream")
def api_run_stream(run_id: str):
    """Server-sent events: the backlog, then every new line as it is printed."""
    run = manager.get(run_id)
    if run is None:
        return jsonify({"error": "no such run"}), 404

    def events():
        q = run.subscribe()
        try:
            while True:
                line = q.get()
                if line is None:
                    yield f"event: end\ndata: {json.dumps(run.summary())}\n\n"
                    return
                yield f"data: {json.dumps(line)}\n\n"
        finally:
            run.unsubscribe(q)
            # A finished run invalidates the tables: its whole point was to
            # change the sheet.
            sheets_data.invalidate()

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/api/settings")
def api_settings():
    return jsonify(settings.describe())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    body = request.get_json(force=True) or {}
    try:
        result = settings.save(body.get("updates") or {})
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    sheets_data.invalidate()          # target tabs may have changed
    return jsonify(result)


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    url = f"http://{host}:{port}"
    print(f"\n  Automation dashboard → {url}\n")
    if os.environ.get("DASHBOARD_OPEN", "1") == "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    # threaded: an SSE stream holds its worker open for the whole run, so a
    # single-threaded server would deadlock the moment a second tab connected.
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
