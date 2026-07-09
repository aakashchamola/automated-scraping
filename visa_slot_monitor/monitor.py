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
import asyncio
import logging
import os
import time

from telethon import TelegramClient, events

import alerts
import config_util
import dispatcher

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("visa_monitor")


async def _watchdog_loop(cfg: dict, state: dict) -> None:
    """Liveness signals so a silently dead monitor never goes unnoticed:
    startup push, periodic heartbeat, and a warning when no channel traffic
    has been seen for suspiciously long (all channels quiet for hours
    usually means a dropped connection, not a quiet day)."""
    mon = cfg.get("monitoring", {})
    heartbeat_secs = mon.get("heartbeat_hours", 24) * 3600
    stale_secs = mon.get("stale_feed_warning_hours", 8) * 3600

    if mon.get("startup_push", True):
        alerts.fire(
            cfg["alerts"],
            "Visa monitor online",
            f"Watching {len(cfg['telegram']['channels'])} channels. You will hear the siren when a slot opens.",
            urgent=False,
        )

    last_heartbeat = time.time()
    stale_warned = False
    while True:
        await asyncio.sleep(300)
        now = time.time()

        if heartbeat_secs > 0 and now - last_heartbeat >= heartbeat_secs:
            alerts.fire(
                cfg["alerts"],
                "Visa monitor heartbeat",
                f"Still running. {state['count']} channel messages seen since last heartbeat.",
                urgent=False,
            )
            state["count"] = 0
            last_heartbeat = now

        if stale_secs > 0:
            quiet_for = now - state["last_msg"]
            if quiet_for >= stale_secs and not stale_warned:
                alerts.fire(
                    cfg["alerts"],
                    "Visa monitor: feed looks stale",
                    f"No messages from ANY channel in {quiet_for / 3600:.1f}h. "
                    "Check the internet connection and that the account is still in the groups.",
                    urgent=False,
                )
                stale_warned = True
            elif quiet_for < stale_secs:
                stale_warned = False


def main() -> None:
    ap = argparse.ArgumentParser(description="Real-time visa slot monitor")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()
    cfg = config_util.load_config(args.config, require_telegram_creds=True)
    tg = cfg["telegram"]

    session_path = os.path.join(_MODULE_DIR, tg.get("session_name", "visa_monitor"))
    client = TelegramClient(session_path, int(tg["api_id"]), tg["api_hash"])
    channels = tg["channels"]
    state = {"last_msg": time.time(), "count": 0}

    @client.on(events.NewMessage(chats=channels))
    async def on_message(event):
        state["last_msg"] = time.time()
        state["count"] += 1
        chat = await event.get_chat()
        channel = getattr(chat, "username", None) or getattr(chat, "title", "?")
        text = event.raw_text or ""
        logger.debug(f"[{channel}] {text[:150]!r}")
        try:
            dispatcher.process_message(cfg, f"t.me/{channel}", text)
        except Exception:
            logger.exception("failed to process message")

    logger.info(f"Watching {len(channels)} channels: {', '.join(channels)}")
    logger.info("Alarm test: python alerts.py --test")
    with client:
        client.loop.create_task(_watchdog_loop(cfg, state))
        client.run_until_disconnected()


if __name__ == "__main__":
    main()
