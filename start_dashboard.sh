#!/usr/bin/env bash
# Launch the automation control panel at http://127.0.0.1:5000
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

exec python -m web.app
