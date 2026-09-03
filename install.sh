#!/usr/bin/env bash
#
# install.sh — set this machine up to run the automation.
#
#     curl -fsSL https://raw.githubusercontent.com/aakashchamola/automated-scraping/automateV2/install.sh | bash
#
# or, having read it first, which is the better habit for anything piped into a
# shell:
#
#     curl -fsSL <that url> -o install.sh && less install.sh && bash install.sh
#
# It installs into ~/automated-scraping, needs no root, and touches nothing
# outside that directory and the Python virtual environment inside it. Running
# it twice is safe: it updates rather than reinstalls.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/aakashchamola/automated-scraping.git}"
BRANCH="${BRANCH:-automateV2}"
TARGET="${TARGET:-$HOME/automated-scraping}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1m→ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m!  %s\033[0m\n' "$1"; }
die()  { printf '\033[31m✗  %s\033[0m\n' "$1" >&2; exit 1; }
ok()   { printf '\033[32m✓\033[0m  %s\n' "$1"; }

bold "Automation setup"
printf '   into %s\n' "$TARGET"

# ── What has to be there already ─────────────────────────────────────────────
step "Checking what this machine has"

command -v git >/dev/null || die "git is not installed. On Debian/Ubuntu:
     sudo apt install git
   On macOS, install the Xcode command line tools: xcode-select --install"

PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null; then
        version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)
        major=${version%%.*}; minor=${version##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 9 ]; then PY="$candidate"; break; fi
    fi
done
[ -n "$PY" ] || die "Python 3.9 or newer is required. On Debian/Ubuntu:
     sudo apt install python3 python3-venv
   On macOS:  brew install python"
ok "$($PY --version)"
ok "$(git --version)"

# venv is a separate package on Debian/Ubuntu and its absence is confusing:
# python is present, the command just fails.
"$PY" -c 'import venv' 2>/dev/null || die "Python's venv module is missing. On Debian/Ubuntu:
     sudo apt install python3-venv"

# ── The code ─────────────────────────────────────────────────────────────────
step "Fetching the code"
if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch --quiet origin "$BRANCH"
    git -C "$TARGET" checkout --quiet "$BRANCH"
    # Reset rather than pull: a local edit should not turn an update into a
    # merge conflict in a script nobody is watching.
    git -C "$TARGET" reset --quiet --hard "origin/$BRANCH"
    ok "updated $TARGET to the latest $BRANCH"
else
    [ -e "$TARGET" ] && die "$TARGET exists but is not a git checkout. Move it aside, or set TARGET=<somewhere else>."
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET"
    ok "cloned into $TARGET"
fi
cd "$TARGET"

# ── Dependencies ─────────────────────────────────────────────────────────────
step "Installing dependencies (a minute or two the first time)"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
ok "$(./.venv/bin/python -m pip list 2>/dev/null | wc -l) packages ready"

# Proves the install rather than assuming it: these are the imports that fail
# first when a wheel did not build.
./.venv/bin/python - <<'PY' || die "the dependencies installed but do not import — see the errors above"
import gspread, requests, bs4, pandas, yaml, flask          # noqa: F401
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
print("  imports ok")
PY
ok "verified"

# ── What is still missing ────────────────────────────────────────────────────
step "Ready"
printf '\n'
bold "Start it with:"
printf '   cd %s && ./start_dashboard.sh\n' "$TARGET"
printf '   then open http://127.0.0.1:5000\n\n'

if [ -f secrets/google-service-account.json ]; then
    ok "Google credentials are in place — the pipeline will use them directly"
    exit 0
fi

cat <<'NOTE'

One more thing, and it is just a password.

  The pipeline needs somewhere to read its keywords and settings from, and
  somewhere to put what it finds. It gets both from the project's web service,
  which means this machine needs NO Google credentials at all — only the
  password you already use to open the dashboard.

  Put these two in your shell (or in a .env file beside this script):

    export SETTINGS_WEB_APP_URL='<the /exec URL from whoever runs the project>'
    export PROJECT_PASSWORD='<your project password>'

  Then run the pipeline as normal:

    ./.venv/bin/python main.py --config config.yaml

  There is deliberately no service-account key to hand out: one key can read
  and write every spreadsheet it has ever been shared with, and there is no way
  to narrow it to a single project. A password reaches one project and nothing
  else, and it can be changed from the dashboard whenever you like.

NOTE
