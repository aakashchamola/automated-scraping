#!/usr/bin/env bash
# One-command start (macOS / Linux): venv + deps + setup wizard (first run) + supervised monitor.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating Python environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# Run the wizard until the phone alert topic is configured
if ! python -c 'import json,sys; sys.exit(0 if json.load(open("config.json"))["alerts"]["ntfy"]["topic"].strip() else 1)'; then
    python setup_wizard.py
fi

exec python run_forever.py "$@"
