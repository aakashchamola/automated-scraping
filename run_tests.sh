#!/usr/bin/env bash
# Every suite, in one command.
#
#   ./run_tests.sh
#
# Three languages, because three different things can break:
#   Python   the pipeline and the project registry
#   node     Settings.gs, against a shim of the Google services — the only way
#            to test it without pasting it into the editor and deploying
#   browser  the published dashboard: JSONP login, IndexedDB sessions, and
#            per-project decryption, none of which can be tested any other way
set -uo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
failed=0

echo "── Python ─────────────────────────────────────────────────────────────"
# ROS installs a pytest plugin that fails to import here; autoload off avoids it.
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PY" -m pytest -q tests/test_*_unit.py || failed=1

echo
echo "── Apps Script ────────────────────────────────────────────────────────"
if command -v node >/dev/null; then
    node apps-script/test_settings.js || failed=1
else
    echo "  skipped: node is not installed"
fi

echo
echo "── Browser ────────────────────────────────────────────────────────────"
# Playwright is not a dependency of this project; point at any install of it.
for candidate in \
    "${PLAYWRIGHT_PATH:-}" \
    node_modules/playwright \
    ../parinama/pgc_parinama/node_modules/playwright \
    ../parinama/pds_parinama/node_modules/playwright
do
    [ -n "$candidate" ] && [ -d "$candidate" ] && {
        PLAYWRIGHT_PATH="$(cd "$candidate" && pwd)"; break; }
done
if [ -n "${PLAYWRIGHT_PATH:-}" ] && [ -d "$PLAYWRIGHT_PATH" ]; then
    PLAYWRIGHT_PATH="$PLAYWRIGHT_PATH" node site/test_browser.js || failed=1
else
    echo "  skipped: playwright not found (set PLAYWRIGHT_PATH, or npm i playwright)"
fi

echo
[ $failed -eq 0 ] && echo "everything passed" || echo "SOMETHING FAILED"
exit $failed
