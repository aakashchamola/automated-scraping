"""
check_previews.py — Discover which Telegram channels can be monitored with
NO api_id/api_hash (i.e. their public web preview at t.me/s/<channel> works).

Use this when you can't create Telegram API credentials. Feed it a list of
candidate channel usernames; it reports which are readable by
web_preview_poller.py. Add the "READABLE" ones to config.json ->
telegram.channels and run:  python run_forever.py --target poller,reddit

Usage:
    python check_previews.py                       # checks channels in config.json
    python check_previews.py chan_a chan_b chan_c  # checks the given usernames
"""

import json
import os
import sys

import requests
from bs4 import BeautifulSoup

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def check(session: requests.Session, channel: str) -> tuple[str, int]:
    """Return (verdict, message_count) for a channel's public web preview."""
    channel = channel.strip().lstrip("@").replace("https://t.me/", "").replace("t.me/", "")
    try:
        resp = session.get(f"https://t.me/s/{channel}", headers=_HEADERS, timeout=15)
    except requests.RequestException as exc:
        return f"ERROR ({exc.__class__.__name__})", 0
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}", 0
    soup = BeautifulSoup(resp.text, "html.parser")
    msgs = soup.select("div.tgme_widget_message")
    if msgs:
        return "READABLE", len(msgs)
    # preview disabled -> page shows only a "join/view in Telegram" prompt
    return "no-preview (needs API creds)", 0


def main() -> None:
    if len(sys.argv) > 1:
        channels = sys.argv[1:]
    else:
        with open(os.path.join(_MODULE_DIR, "config.json"), encoding="utf-8") as fh:
            channels = json.load(fh)["telegram"]["channels"]

    session = requests.Session()
    print(f"Checking {len(channels)} channel(s) for credential-free readability:\n")
    readable = []
    for ch in channels:
        verdict, count = check(session, ch)
        flag = "OK " if verdict == "READABLE" else "   "
        extra = f" ({count} messages visible)" if count else ""
        print(f"  {flag}{ch:34} {verdict}{extra}")
        if verdict == "READABLE":
            readable.append(ch.strip().lstrip("@").replace("https://t.me/", "").replace("t.me/", ""))

    print()
    if readable:
        print("These work with the no-login poller — put them in config.json -> "
              "telegram.channels:")
        print("  " + json.dumps(readable))
        print("Then run:  python run_forever.py --target poller,reddit")
    else:
        print("None are readable without API credentials. Either fix my.telegram.org "
              "(see RUN_AND_TEST.md) or rely on Reddit:  python run_forever.py --target reddit")


if __name__ == "__main__":
    main()
