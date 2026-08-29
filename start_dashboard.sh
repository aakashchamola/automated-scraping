#!/usr/bin/env bash
# Launch the automation control panel.
#
#   ./start_dashboard.sh              → http://127.0.0.1:5000
#   ./start_dashboard.sh 8080         → a different port
#   PORT=8080 ./start_dashboard.sh    → same thing
#
# Ctrl-C stops it. Everything it needs is set up on first run.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-${PORT:-5000}}"
URL="http://127.0.0.1:${PORT}"

say() { printf '\033[36m→\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── The dashboard lives on automateV2 ────────────────────────────────────────
# web/ does not exist on the visa-monitor branch, and the failure it produces
# there is a confusing TemplateNotFound rather than an obvious wrong-branch
# message. Say so plainly instead.
if [ ! -d web ]; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    if [ "$branch" = "automateV2" ]; then
        die "web/ is missing on automateV2, where it should exist. Restore it with:
    git checkout -- web"
    fi
    die "web/ is missing — you are on branch '${branch}'. The dashboard lives on automateV2:
    git checkout automateV2 && ./start_dashboard.sh"
fi

# ── Google Sheets credentials ────────────────────────────────────────────────
[ -f secrets/google-service-account.json ] || die \
    "secrets/google-service-account.json is missing — the Data tab cannot read the sheet without it."

# ── Port ─────────────────────────────────────────────────────────────────────
# A previous run left in the background is the usual cause; killing a stranger's
# process would be worse than refusing, so report the PID and let the user decide.
if command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    pid="$(lsof -ti "tcp:${PORT}" | head -1)"
    die "port ${PORT} is already in use by PID ${pid}.
    Reuse it:  open ${URL}
    Stop it:   kill ${pid}
    Or:        ./start_dashboard.sh $((PORT + 1))"
fi

# ── Environment ──────────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
    say "creating .venv (first run, takes a minute)"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Only reinstall when requirements.txt is newer than the last successful install
STAMP=.venv/.requirements-installed
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
    say "installing dependencies"
    pip install -q -r requirements.txt
    touch "$STAMP"
fi

say "starting on ${URL}"
say "target tabs: $(python - <<'PY' 2>/dev/null || echo 'unreadable — check config.yaml'
import yaml
c = yaml.safe_load(open("config.yaml"))["google_sheets"]
print(f"{c['jobs_worksheet']} (jobs), {c['enrichment_output_worksheet']} (companies)")
PY
)"

export DASHBOARD_PORT="$PORT"
exec python -m web.app
