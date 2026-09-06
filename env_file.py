"""Reading the two settings from a file, so nothing has to be exported.

install.sh asks for the project's password once and writes it here, beside the
code, and every entry point picks it up from there. Without this the answers
lived only in whichever shell typed them: close the terminal and the agent
stops working with no explanation, which is exactly the failure a one-command
install is meant to remove.

── WHY NOT python-dotenv ────────────────────────────────────────────────────
It is one more thing to install before the thing that installs things works,
for thirty lines of parsing. This handles the shapes a person actually
produces — a pasted `export FOO=bar`, quotes, a comment, a stray blank line —
and ignores everything else it does not understand rather than failing.

That last part is not hypothetical: this repository has carried a `.env` full
of scraped links for months. A parser that raised on it would have made the
whole pipeline unstartable.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Beside the code, never the working directory. The agent launches every run
# with a cwd of its own choosing and systemd starts it from $HOME unless told
# otherwise, so "the current directory" is not somewhere a person can point to.
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# What this file is allowed to define. An allowlist, because a .env is a file
# the operator edits by hand and a typo should not quietly become an
# environment variable that changes something unrelated.
KNOWN = ("SETTINGS_WEB_APP_URL", "PROJECT_PASSWORD", "PROJECT_ID",
         "CONTROL_SPREADSHEET_ID", "LOG_CONSOLE_LEVEL")

_loaded = False


def parse(text: str) -> dict:
    """The assignments in *text*. Anything else is skipped in silence."""
    found = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue                       # not an assignment; not our business
        name = name.strip()
        if not name.replace("_", "").isalnum():
            continue                       # a sentence with an = in it, not a name
        value = value.strip()
        # Quotes are how a value keeps a space at either end, so they are
        # stripped only as a matched pair and only from the outside.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        found[name] = value
    return found


def load(path: str = None) -> dict:
    """Apply the file to os.environ, filling only what is not already set.

    An exported variable always wins: CI sets these deliberately, and agent.py
    passes them down to every run it starts. Empty counts as unset, because
    every reader here treats an empty string as "not configured" — an
    `export PROJECT_PASSWORD=` left in a shell profile would otherwise defeat
    the file and send the machine looking for a Google key it does not have.
    """
    target = path or ENV_PATH
    try:
        # utf-8-sig: a file saved by a Windows editor starts with a byte-order
        # mark, and it would otherwise be read as part of the first name — so
        # the first variable, the URL, would silently not exist.
        with open(target, encoding="utf-8-sig") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return {}

    applied = {}
    for name, value in parse(text).items():
        if name not in KNOWN:
            continue
        if os.environ.get(name):
            continue
        os.environ[name] = value
        applied[name] = value
    return applied


def load_once() -> dict:
    """load(), at most once per process."""
    global _loaded
    if _loaded:
        return {}
    _loaded = True
    return load()
