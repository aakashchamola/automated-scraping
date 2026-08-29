"""
channel_health.py — Which watched channels are actually worth watching?

Pulls recent history from every configured Telegram channel and reports, per
channel: how long since the last post, posting rate, how much of the traffic is
unique (a low ratio means one ad on repeat), and — the number that matters —
how many messages are *genuine structured slot updates* for a watched consulate
versus advertising and chatter.

Channels go dead quietly. Two of the five originally configured here had not
posted since 2024/2025 and would never have fired, while the busiest one turned
out to be a single advertisement re-posted every few hours. Run this before
trusting the monitor's silence:

    python channel_health.py
    python channel_health.py --limit 200 --json report.json
"""

import argparse
import asyncio
import collections
import json
import os
from datetime import datetime, timezone

from telethon import TelegramClient

import config_util
import slot_parser

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


def _summarise(channel: str, messages: list, filter_cfg: dict) -> dict:
    """Signal/noise breakdown for one channel's recent history."""
    row = {
        "channel": channel, "messages": len(messages), "last_post_age_days": None,
        "span_days": None, "posts_per_day": 0.0, "unique_ratio": 0.0,
        "structured_updates": 0, "watched_consulate_updates": 0,
        "fresh_updates": 0, "free_text_hits": 0, "verdict": "no messages",
    }
    if not messages:
        row["verdict"] = "DEAD — no messages readable"
        return row

    now = datetime.now(timezone.utc)
    newest, oldest = messages[0]["date"], messages[-1]["date"]
    row["last_post_age_days"] = round((now - newest).total_seconds() / 86400, 1)
    span_days = max((newest - oldest).total_seconds() / 86400, 1e-9)
    row["span_days"] = round(span_days, 1)
    row["posts_per_day"] = round(len(messages) / max(span_days, 1 / 24), 1)

    bodies = [" ".join(m["text"].split()) for m in messages]
    row["unique_ratio"] = round(len(set(bodies)) / len(bodies), 2)

    for message in messages:
        text = message["text"]
        if slot_parser.parse_bot_update(text):
            row["structured_updates"] += 1
        detection = slot_parser.classify(text, filter_cfg)
        if not detection:
            continue
        if detection.get("format") == "bot_update":
            row["watched_consulate_updates"] += 1
            if not detection.get("stale"):
                row["fresh_updates"] += 1
        else:
            row["free_text_hits"] += 1

    if row["last_post_age_days"] > 90:
        row["verdict"] = f"DEAD — silent {row['last_post_age_days']:.0f} days, remove it"
    elif row["watched_consulate_updates"]:
        row["verdict"] = f"USEFUL — {row['watched_consulate_updates']} watched-consulate updates"
    elif row["structured_updates"]:
        row["verdict"] = (f"PARTIAL — {row['structured_updates']} bot updates, "
                          "none for a watched consulate")
    elif row["unique_ratio"] < 0.2:
        row["verdict"] = "NOISE — one message on repeat (advertising)"
    else:
        row["verdict"] = "NOISE — chatter/ads only, no structured updates"
    return row


async def collect(cfg: dict, limit: int) -> list:
    tg = cfg["telegram"]
    client = TelegramClient(
        os.path.join(_MODULE_DIR, tg.get("session_name", "visa_monitor")),
        int(tg["api_id"]), tg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "Telegram session is not authorized. Run: python monitor.py (it will "
            "ask for your phone number and OTP once).")

    rows = []
    for channel in tg["channels"]:
        try:
            entity = await client.get_entity(channel)
            messages = [
                {"date": m.date, "text": m.raw_text}
                async for m in client.iter_messages(entity, limit=limit)
                if m.raw_text
            ]
        except Exception as exc:                      # channel gone / not joined
            rows.append({"channel": channel, "messages": 0,
                         "verdict": f"UNREACHABLE — {type(exc).__name__}: {exc}"})
            continue
        rows.append(_summarise(channel, messages, cfg["filter"]))
    await client.disconnect()
    return rows


def _print_report(rows: list) -> None:
    print(f"\n{'CHANNEL':<34} {'LAST':>7} {'/DAY':>6} {'UNIQ':>5} {'BOT':>4} {'MINE':>5} {'FRESH':>6}  VERDICT")
    print("-" * 118)
    for r in rows:
        last = f"{r['last_post_age_days']}d" if r.get("last_post_age_days") is not None else "-"
        print(f"{r['channel']:<34} {last:>7} {r.get('posts_per_day', 0):>6} "
              f"{r.get('unique_ratio', 0):>5} {r.get('structured_updates', 0):>4} "
              f"{r.get('watched_consulate_updates', 0):>5} {r.get('fresh_updates', 0):>6}  {r['verdict']}")
    print("\nBOT   = messages in the structured notifier-bot format")
    print("MINE  = of those, ones for a consulate on filter.watch_consulates")
    print("FRESH = of those, ones inside filter.max_alert_age_minutes (would have sirened)")

    useful = [r for r in rows if r["verdict"].startswith("USEFUL")]
    if not useful:
        print("\n⚠  No watched channel produced a single actionable slot update in this "
              "window. The monitor is running correctly and there is simply nothing to "
              "hear — treat its silence as 'no signal available', not 'no slots'.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Report signal/noise per watched channel")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    ap.add_argument("--limit", type=int, default=100, help="messages to sample per channel")
    ap.add_argument("--json", help="also write the report to this JSON file")
    args = ap.parse_args()

    cfg = config_util.load_config(args.config, require_telegram_creds=True)
    rows = asyncio.run(collect(cfg, args.limit))
    _print_report(rows)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
