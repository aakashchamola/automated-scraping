"""
web_preview_poller.py — Fallback monitor that needs NO Telegram account.

Polls each channel's public web preview (https://t.me/s/<channel>) and feeds
new messages through the same parser/alert pipeline as monitor.py. Useful
when you can't create Telegram API credentials, or to run on a phone via
Termux. Trade-offs vs monitor.py:

  * ~interval_seconds of extra latency (default 45s) instead of instant push
  * some channels disable the web preview entirely — verified July 2026:
    the default slot-tracker channels in config.json have it DISABLED, so
    this poller cannot see them and monitor.py is the required path for
    them. The poller warns loudly per channel when the preview is empty.

Run:
    python web_preview_poller.py
"""

import argparse
import json
import logging
import os
import time

import requests
from bs4 import BeautifulSoup

import dispatcher

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_SEEN_PATH = os.path.join(_MODULE_DIR, "logs", "seen_ids.json")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("preview_poller")


def load_seen() -> dict[str, list[int]]:
    try:
        with open(_SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict[str, list[int]]) -> None:
    os.makedirs(os.path.dirname(_SEEN_PATH), exist_ok=True)
    # keep only the most recent ids per channel so the file stays small
    trimmed = {ch: sorted(ids)[-200:] for ch, ids in seen.items()}
    with open(_SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(trimmed, fh)


def fetch_messages(session: requests.Session, channel: str) -> list[tuple[int, str]]:
    """Return [(message_id, text)] from the channel's public preview page."""
    url = f"https://t.me/s/{channel}"
    resp = session.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for wrap in soup.select("div.tgme_widget_message"):
        post = wrap.get("data-post", "")  # e.g. "channelname/12345"
        try:
            msg_id = int(post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        text_el = wrap.select_one("div.tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if text:
            out.append((msg_id, text))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="No-login visa slot monitor (web preview polling)")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    channels = cfg["telegram"]["channels"]
    interval = cfg.get("web_preview_poll", {}).get("interval_seconds", 45)
    seen = load_seen()
    session = requests.Session()
    first_pass = {ch: ch not in seen for ch in channels}
    warned_empty: set[str] = set()

    logger.info(f"Polling {len(channels)} channels every {interval}s: {', '.join(channels)}")
    while True:
        for channel in channels:
            try:
                messages = fetch_messages(session, channel)
            except requests.RequestException as exc:
                logger.warning(f"[{channel}] fetch failed: {exc}")
                continue
            if not messages:
                if channel not in warned_empty:
                    warned_empty.add(channel)
                    logger.warning(
                        f"[{channel}] web preview is empty/disabled — this channel "
                        "CANNOT be monitored here. Use monitor.py (real-time mode) for it."
                    )
                continue
            known = set(seen.setdefault(channel, []))
            for msg_id, text in messages:
                if msg_id in known:
                    continue
                known.add(msg_id)
                seen[channel].append(msg_id)
                # First pass just baselines history — don't alarm on old posts
                if not first_pass[channel]:
                    try:
                        dispatcher.process_message(cfg, channel, text)
                    except Exception:
                        logger.exception("failed to process message")
            first_pass[channel] = False
        save_seen(seen)
        time.sleep(interval)


if __name__ == "__main__":
    main()
