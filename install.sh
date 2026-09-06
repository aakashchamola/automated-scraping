#!/usr/bin/env bash
#
# install.sh — set this machine up to run the automation, in one command.
#
#     curl -fsSL https://raw.githubusercontent.com/aakashchamola/automated-scraping/automateV2/install.sh | bash
#
# It clones into ~/automated-scraping, builds a Python virtual environment,
# asks for your project password, checks the password actually works, saves it,
# and offers to start the agent. Nothing to export afterwards, and nothing to
# remember: close the terminal and it still knows.
#
# Needs no root. Touches nothing outside ~/automated-scraping. Safe to run
# again — it updates, and keeps the password you already gave it.
#
# Not interactive (CI, a provisioning script, no terminal)? Pass the answers:
#     curl -fsSL <url> | bash -s -- --password 'the project password'
#     curl -fsSL <url> | bash -s -- --url 'https://…/exec' --password '…' --start
#
# ── WHY THE PROMPT IS WRITTEN THE WAY IT IS ─────────────────────────────────
# When this is piped into bash, bash reads the script from stdin — one byte at
# a time — so a plain `read` swallows the script's own next line and that line
# never runs. `exec < /dev/tty` at the top is worse: it replaces the source of
# the script, and everything after it is read from the keyboard. The terminal
# is therefore opened once on fd 3 and each prompt reads from there, which also
# gives a single, testable answer to "is anybody actually there".
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/aakashchamola/automated-scraping.git}"
BRANCH="${BRANCH:-automateV2}"
TARGET="${TARGET:-$HOME/automated-scraping}"

# The project's service. Baked in so nobody has to be sent it separately — it
# is on the dashboard page too. It is not a secret: it opens nothing without a
# password, and every password reaches exactly one project.
DEFAULT_EXEC_URL="https://script.google.com/macros/s/AKfycbybBHsDZhC0Wb-LT9SruhqGJyeVCZaMUrU6n1gFpK21AbzDofzR76AbhmTg4sXtn_U/exec"

EXEC_URL="${SETTINGS_WEB_APP_URL:-$DEFAULT_EXEC_URL}"
PASSWORD="${PROJECT_PASSWORD:-}"
START_AGENT="ask"

while [ $# -gt 0 ]; do
    case "$1" in
        --url)      EXEC_URL="${2:-}"; shift 2 ;;
        --password) PASSWORD="${2:-}"; shift 2 ;;
        --start)    START_AGENT="yes"; shift ;;
        --no-start) START_AGENT="no";  shift ;;
        -h|--help)  sed -n '2,25p' "$0" 2>/dev/null || true; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

# Colour only when something can render it.
if [ -t 2 ]; then B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; OFF=$'\033[0m'
else B=''; DIM=''; RED=''; GRN=''; YEL=''; OFF=''; fi

# Everything the person is meant to read goes to stderr, so it survives
# `| tee install.log` and stays with the prompts.
say()  { printf '%s\n' "$1" >&2; }
bold() { printf '%s%s%s\n' "$B" "$1" "$OFF" >&2; }
step() { printf '\n%s→ %s%s\n' "$B" "$1" "$OFF" >&2; }
warn() { printf '%s!  %s%s\n' "$YEL" "$1" "$OFF" >&2; }
die()  { printf '%s✗  %s%s\n' "$RED" "$1" "$OFF" >&2; exit 1; }
ok()   { printf '%s✓%s  %s\n' "$GRN" "$OFF" "$1" >&2; }

# Can we ask anything? The braces matter: redirections are applied left to
# right, so without them the error message escapes before 2>/dev/null is in
# effect — and under `set -e` a bare failing exec would end the script here.
if { exec 3< /dev/tty; } 2>/dev/null; then INTERACTIVE=1; else INTERACTIVE=0; fi

bold "Automation setup"
say  "   into $TARGET"

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

"$PY" -c 'import venv' 2>/dev/null || die "Python's venv module is missing. On Debian/Ubuntu:
     sudo apt install python3-venv"

# ── The code ─────────────────────────────────────────────────────────────────
# Every child gets an explicit stdin. Without it they inherit the curl pipe and
# read the rest of the script, which then silently never runs.
step "Fetching the code"
if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch --quiet origin "$BRANCH" < /dev/null
    git -C "$TARGET" checkout --quiet "$BRANCH" < /dev/null
    # Reset rather than pull: a local edit should not turn an update into a
    # merge conflict in a script nobody is watching. .env is untracked and
    # ignored, so the saved password survives this.
    git -C "$TARGET" reset --quiet --hard "origin/$BRANCH" < /dev/null
    ok "updated $TARGET to the latest $BRANCH"
else
    [ -e "$TARGET" ] && die "$TARGET exists but is not a git checkout. Move it aside, or set TARGET=<somewhere else>."
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET" < /dev/null
    ok "cloned into $TARGET"
fi
cd "$TARGET"

# ── Dependencies ─────────────────────────────────────────────────────────────
step "Installing dependencies (a minute or two the first time)"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip < /dev/null
./.venv/bin/python -m pip install --quiet -r requirements.txt < /dev/null
ok "$(./.venv/bin/python -m pip list 2>/dev/null | wc -l) packages ready"

./.venv/bin/python - <<'PY' >&2 || die "the dependencies installed but do not import — see the errors above"
import gspread, requests, bs4, pandas, yaml, flask          # noqa: F401
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
print("  imports ok")
PY
ok "verified"

# ── The one question ─────────────────────────────────────────────────────────
# Asked last, on purpose: everything above can be got on with while somebody
# goes and finds their password.

# A password already saved from a previous run is reused rather than asked for
# again, which is what makes re-running this safe.
if [ -z "$PASSWORD" ] && [ -f .env ]; then
    SAVED=$(./.venv/bin/python -c '
import env_file
print(env_file.parse(open(".env", encoding="utf-8-sig", errors="replace").read())
      .get("PROJECT_PASSWORD", ""))' 2>/dev/null || true)
    if [ -n "${SAVED:-}" ]; then
        PASSWORD="$SAVED"
        ok "using the password already saved here"
    fi
fi

# Checks a password against the service, printing nothing that came back from
# it. Exit 0 good, 1 refused, 2 could not tell.
check_password() {
    ./.venv/bin/python - "$1" "$2" <<'PY' >&2
import sys
try:
    import requests
except Exception:
    sys.exit(2)
url, password = sys.argv[1], sys.argv[2]
try:
    reply = requests.get(url, params={"action": "auth", "password": password},
                         timeout=45)
    reply.raise_for_status()
    body = reply.text.strip()
    if body.startswith("<"):
        print("  the service answered with a web page, not data")
        sys.exit(2)
    import json
    # Never print the reply: on success it carries this project's data key.
    payload = json.loads(body)
except Exception as exc:
    # The exception text would quote the URL, and the URL carries the password.
    print(f"  could not reach the service ({type(exc).__name__})")
    sys.exit(2)
if payload.get("ok"):
    print(f"  opens: {payload.get('name') or payload.get('project')}")
    sys.exit(0)
print("  that password did not open any project")
sys.exit(1)
PY
}

step "Your project password"
if [ -z "$PASSWORD" ] && [ "$INTERACTIVE" = 1 ]; then
    say "${DIM}The same one you use to sign in to the dashboard. It is shown as"
    say "you type — nobody else can see this terminal.${OFF}"
    attempt=1
    while [ "$attempt" -le 3 ]; do
        printf '\n  password: ' >&2
        # Deliberately not `read -s`: you asked to see what you are typing, and
        # a password you can read back is one you can tell you mistyped.
        # (`read -s -u 3` would also echo it anyway on the bash macOS ships.)
        typed=""
        read -r typed <&3 || true
        if [ -z "$typed" ]; then
            warn "nothing typed"
            attempt=$((attempt + 1)); continue
        fi
        rc=0; check_password "$EXEC_URL" "$typed" || rc=$?
        if [ "$rc" -eq 0 ]; then
            PASSWORD="$typed"; break
        elif [ "$rc" -eq 2 ]; then
            # The network, not the answer. Keep what was typed.
            warn "could not check it just now — saving it anyway"
            PASSWORD="$typed"; break
        fi
        attempt=$((attempt + 1))
        [ "$attempt" -le 3 ] && warn "try again ($((4 - attempt)) left)"
    done
elif [ -n "$PASSWORD" ]; then
    rc=0; check_password "$EXEC_URL" "$PASSWORD" || rc=$?
    [ "$rc" -eq 1 ] && warn "that password did not open any project — saving it anyway, edit $TARGET/.env to change it"
fi

# ── Saving it ────────────────────────────────────────────────────────────────
if [ -n "$PASSWORD" ]; then
    # 0600 before anything is written to it: on a shared machine every other
    # account can read a file created under the usual umask.
    ( umask 077
      {
        printf '# Written by install.sh. This machine needs no Google key —\n'
        printf '# only these two lines, which reach one project and nothing else.\n'
        printf 'SETTINGS_WEB_APP_URL=%s\n' "$EXEC_URL"
        printf 'PROJECT_PASSWORD=%s\n' "$PASSWORD"
      } > .env )
    chmod 600 .env 2>/dev/null || true
    ok "saved to $TARGET/.env (readable only by you)"
    ok "nothing to export — every command here reads that file"
else
    warn "no password given, so nothing was saved"
    say  ""
    say  "  Put it in $TARGET/.env when you have it:"
    say  "    SETTINGS_WEB_APP_URL=$EXEC_URL"
    say  "    PROJECT_PASSWORD=your project password"
    say  ""
    say  "  Then: cd $TARGET && ./.venv/bin/python agent.py"
    exit 0
fi

# ── Starting it ──────────────────────────────────────────────────────────────
step "The agent"
say "${DIM}This is what connects the Run buttons on the dashboard to this"
say "machine. Leave it running: the dashboard will say your machine is"
say "listening, and Run there starts the work here.${OFF}"

if [ "$START_AGENT" = "ask" ] && [ "$INTERACTIVE" = 1 ]; then
    printf '\n  start it now? [Y/n] ' >&2
    answer=""
    read -r answer <&3 || true
    case "${answer:-y}" in [Nn]*) START_AGENT="no" ;; *) START_AGENT="yes" ;; esac
elif [ "$START_AGENT" = "ask" ]; then
    START_AGENT="no"
fi

if [ "$START_AGENT" = "yes" ]; then
    say ""
    ok "starting — press Ctrl-C to stop"
    say ""
    # exec, so Ctrl-C reaches the agent rather than this script.
    exec ./.venv/bin/python agent.py
fi

say ""
bold "Start it whenever you like:"
say  "   cd $TARGET && ./.venv/bin/python agent.py"
say  ""
say  "${DIM}Or run one part on its own — same file, no agent needed:"
say  "   ./.venv/bin/python main.py --config config.yaml               # scrape"
say  "   ./.venv/bin/python job_validator.py --config config.yaml      # check links"
say  "   ./.venv/bin/python automation_pipeline.py --config config.yaml  # all of it${OFF}"
