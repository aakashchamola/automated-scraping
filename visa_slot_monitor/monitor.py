"""
monitor.py — Real-time F1 visa slot monitor (primary entry point).

Listens live to the configured public Telegram channels with your own
Telegram account (via Telethon) and rings the alarm the moment a message
looks like a slot opening. Booking stays manual — this only alerts.

Setup (one time):
    1. Get api_id + api_hash at https://my.telegram.org (API development tools)
       and put them in config.json under "telegram".
    2. Join the channels listed in config.json from your Telegram app.
    3. First run asks for your phone number + OTP, then stays logged in
       (visa_monitor.session file — keep it private, it is your login).

Run:
    python monitor.py
    python monitor.py --config config.json
"""

import argparse
import json
import logging
import os
import sys

from telethon import TelegramClient, events

import dispatcher

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("visa_monitor")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    tg = cfg.get("telegram", {})
    if not tg.get("api_id") or not tg.get("api_hash"):
        logger.error(
            "telegram.api_id / api_hash missing in config.json.\n"
            "Get them at https://my.telegram.org -> 'API development tools', "
            "then paste into config.json. (No-login alternative: web_preview_poller.py)"
        )
        sys.exit(1)
    if not tg.get("channels"):
        logger.error("telegram.channels is empty in config.json")
        sys.exit(1)
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Real-time visa slot monitor")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    tg = cfg["telegram"]

    session_path = os.path.join(_MODULE_DIR, tg.get("session_name", "visa_monitor"))
    client = TelegramClient(session_path, int(tg["api_id"]), tg["api_hash"])
    channels = tg["channels"]

    @client.on(events.NewMessage(chats=channels))
    async def on_message(event):
        chat = await event.get_chat()
        channel = getattr(chat, "username", None) or getattr(chat, "title", "?")
        text = event.raw_text or ""
        logger.debug(f"[{channel}] {text[:150]!r}")
        try:
            dispatcher.process_message(cfg, channel, text)
        except Exception:
            logger.exception("failed to process message")

    logger.info(f"Watching {len(channels)} channels: {', '.join(channels)}")
    logger.info("Alarm test: python alerts.py --test")
    with client:
        client.run_until_disconnected()


if __name__ == "__main__":
    main()
