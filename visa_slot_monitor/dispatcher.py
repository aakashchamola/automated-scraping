"""
dispatcher.py — Shared pipeline between the two entry points (Telethon
monitor and web-preview poller): classify a message, apply the per-consulate
cooldown, fire alerts, and append to the history CSV.
"""

import csv
import hashlib
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

# normalized-text hash -> [timestamps]; ad templates repeat verbatim many
# times a day, genuine notifications differ (date/count changes each time)
_template_seen: dict[str, list[float]] = {}
_TEMPLATE_WINDOW = 86400  # 24h
_TEMPLATE_MAX_REPEATS = 2  # 3rd identical message within the window is spam


def _is_repeated_template(text: str) -> bool:
    key = hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()
    now = time.time()
    hits = [t for t in _template_seen.get(key, []) if now - t < _TEMPLATE_WINDOW]
    hits.append(now)
    _template_seen[key] = hits
    if len(_template_seen) > 5000:  # bound memory on long runs
        oldest = sorted(_template_seen, key=lambda k: _template_seen[k][-1])[:1000]
        for k in oldest:
            del _template_seen[k]
    return len(hits) > _TEMPLATE_MAX_REPEATS


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
    if _is_repeated_template(text):
        logger.info(f"[{channel}] repeated ad template suppressed: {text[:120]!r}")
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
    body_lines = [f"Source: {channel}"]
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
