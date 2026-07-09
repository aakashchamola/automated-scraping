"""
reddit_source.py — Redundant slot-opening source: Reddit new-post polling.

Students post "slots open at <consulate> right now!!" on subreddits like
r/f1visa within minutes of an opening. Latency is worse than the Telegram
bot channels, but it needs no account at all and catches openings the
groups miss. Runs alongside monitor.py (see run_forever.py --target).

Uses Reddit's public JSON listing (no API key): /r/<sub>/new.json.
Polite polling (default 90s per cycle) with a descriptive User-Agent.
"""

import argparse
import json
import logging
import os
import time

import requests

import config_util
import dispatcher

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_SEEN_PATH = os.path.join(_MODULE_DIR, "logs", "reddit_seen.json")
_HEADERS = {"User-Agent": "visa-slot-alert-tool/1.0 (personal notification use)"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reddit_source")


def load_seen() -> dict[str, list[str]]:
    try:
        with open(_SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict[str, list[str]]) -> None:
    os.makedirs(os.path.dirname(_SEEN_PATH), exist_ok=True)
    trimmed = {sub: ids[-300:] for sub, ids in seen.items()}
    with open(_SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(trimmed, fh)


def fetch_new_posts(session: requests.Session, subreddit: str) -> list[tuple[str, str]]:
    """Return [(post_id, text)] for the newest posts in a subreddit."""
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
    resp = session.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    out = []
    for child in resp.json().get("data", {}).get("children", []):
        data = child.get("data", {})
        post_id = data.get("id")
        title = data.get("title", "")
        body = data.get("selftext", "")[:800]
        if post_id and title:
            out.append((post_id, f"{title}\n{body}".strip()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Reddit slot-post monitor")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()
    cfg = config_util.load_config(args.config)

    reddit_cfg = cfg.get("reddit", {})
    subreddits = reddit_cfg.get("subreddits", ["f1visa", "usvisascheduling"])
    interval = reddit_cfg.get("interval_seconds", 90)

    seen = load_seen()
    session = requests.Session()
    first_pass = {sub: sub not in seen for sub in subreddits}

    logger.info(f"Polling r/{', r/'.join(subreddits)} every {interval}s")
    while True:
        for sub in subreddits:
            try:
                posts = fetch_new_posts(session, sub)
            except (requests.RequestException, ValueError) as exc:
                logger.warning(f"[r/{sub}] fetch failed: {exc}")
                continue
            known = set(seen.setdefault(sub, []))
            for post_id, text in posts:
                if post_id in known:
                    continue
                known.add(post_id)
                seen[sub].append(post_id)
                if not first_pass[sub]:
                    try:
                        dispatcher.process_message(cfg, f"reddit.com/r/{sub}", text)
                    except Exception:
                        logger.exception("failed to process post")
            first_pass[sub] = False
        save_seen(seen)
        time.sleep(interval)


if __name__ == "__main__":
    main()
