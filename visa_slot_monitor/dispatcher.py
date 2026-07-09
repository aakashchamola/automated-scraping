"""
dispatcher.py — Shared pipeline between the two entry points (Telethon
monitor and web-preview poller): classify a message, apply the per-consulate
cooldown, fire alerts, and append to the history CSV.
"""

import csv
import logging
import os
import time
from datetime import datetime

import alerts
import slot_parser

logger = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.path.join(_MODULE_DIR, "logs")
_HISTORY_CSV = os.path.join(_LOG_DIR, "alerts_history.csv")

# consulate name (or "unknown") -> unix time of last urgent alarm
_last_alarm: dict[str, float] = {}


def _append_history(row: list[str]) -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    new_file = not os.path.exists(_HISTORY_CSV)
    with open(_HISTORY_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["timestamp", "channel", "confidence", "consulates", "dates", "urgent", "message"])
        writer.writerow(row)


def process_message(cfg: dict, channel: str, text: str) -> bool:
    """Classify one message; alert if it looks like a slot opening.
    Returns True if an alert was fired."""
    detection = slot_parser.classify(text, cfg["filter"])
    if detection is None:
        return False
    if detection["confidence"] == "medium" and not cfg["filter"].get("alert_on_uncertain", True):
        logger.info(f"[{channel}] medium-confidence message skipped: {text[:120]!r}")
        return False

    consulates = detection["consulates"] or ["unknown"]
    dates = detection["dates"]

    # Same consulate posted across several groups within the cooldown window:
    # still push to the phone, but quietly — no repeated siren.
    now = time.time()
    cooldown = cfg["alerts"].get("cooldown_seconds", 180)
    urgent = any(now - _last_alarm.get(c, 0) > cooldown for c in consulates)
    if urgent:
        for c in consulates:
            _last_alarm[c] = now

    place = ", ".join(consulates).upper()
    title = f"VISA SLOT: {place}" if place != "UNKNOWN" else "VISA SLOT (location unclear)"
    body_lines = [f"Source: t.me/{channel}"]
    if dates:
        body_lines.append(f"Dates mentioned: {', '.join(dates)}")
    if detection["visa_types"]:
        body_lines.append(f"Visa types: {', '.join(detection['visa_types']).upper()}")
    body_lines.append("")
    body_lines.append(text[:600])
    body = "\n".join(body_lines)

    logger.info(f"ALERT [{channel}] {title} confidence={detection['confidence']} urgent={urgent}")
    alerts.fire(cfg["alerts"], title, body, urgent=urgent)
    _append_history([
        datetime.now().isoformat(timespec="seconds"),
        channel,
        detection["confidence"],
        ";".join(consulates),
        ";".join(dates),
        str(urgent),
        text[:600].replace("\n", " "),
    ])
    return True
