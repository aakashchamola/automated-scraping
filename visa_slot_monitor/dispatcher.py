"""
dispatcher.py — Shared pipeline between all source collectors: classify a
message, score it, dedupe across sources AND processes, fire alerts, and
append to the history CSV.

Scoring: parser confidence (high=2, medium=1) + source reputation (1-3,
config: sources.reputation). A candidate only rings the siren when its
score reaches sources.min_urgent_score and no alarm for the same consulate
fired within alerts.cooldown_seconds; otherwise it degrades to a quiet
phone push. So a high-confidence post in a trusted bot channel alarms
instantly, while a vague mention in a low-trust group just pings.

Dedupe state (cooldowns + seen templates) is persisted to
logs/dispatcher_state.json and re-read on every candidate alert. Sources
run as separate processes (run_forever.py), so this is what guarantees a
slot seen by monitor.py AND reddit_source.py produces one siren, not two —
and that a restart doesn't re-alarm. Alerts are rare; the read-modify-write
cost is irrelevant.

Two gates matter most in practice, because the notifier bots re-post the
*same* availability every few minutes with only their timestamp changed:

  * **Payload dedupe** — a structured update is keyed on what it actually
    says (consulate + month + day list), not on the raw text. Hashing raw
    text is useless here: the moving Time-stamp line makes every repeat a
    "new" message.
  * **Freshness** — a slot that a delayed channel reports 30 minutes late is
    already gone. The bot's own observation timestamp decides whether this
    rings the siren or degrades to a quiet, clearly-labelled STALE push.
"""

import csv
import hashlib
import json
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
_STATE_PATH = os.path.join(_LOG_DIR, "dispatcher_state.json")

_TEMPLATE_WINDOW = 86400  # identical text repeating inside 24h = ad template
_PAYLOAD_WINDOW = 3600    # same consulate+month+days re-announced inside 1h = repeat
_TEMPLATE_MAX_REPEATS = 2  # the 3rd identical message is suppressed
_CONFIDENCE_POINTS = {"high": 2, "medium": 1}


# ── Persistent cross-process state ───────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("last_alarm", {})
        state.setdefault("templates", {})
        state.setdefault("payloads", {})
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_alarm": {}, "templates": {}, "payloads": {}}


def _save_state(state: dict) -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, _STATE_PATH)


def _prune_state(state: dict, now: float) -> None:
    state["payloads"] = {
        key: t for key, t in state.get("payloads", {}).items()
        if now - t < _PAYLOAD_WINDOW
    }
    state["templates"] = {
        key: [t for t in hits if now - t < _TEMPLATE_WINDOW]
        for key, hits in state["templates"].items()
        if any(now - t < _TEMPLATE_WINDOW for t in hits)
    }


def _is_repeated_template(state: dict, text: str, now: float) -> bool:
    key = hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()
    hits = state["templates"].get(key, [])
    hits.append(now)
    state["templates"][key] = hits
    return len(hits) > _TEMPLATE_MAX_REPEATS


def _payload_key(detection: dict) -> str | None:
    """Stable identity of *what slots were announced*, ignoring when.

    Bot #1527 posted the identical "KARACHI / January 2027 / 20,21,26,28,29"
    availability eight times in twenty minutes; only its Time-stamp line
    differed, so raw-text hashing saw eight distinct messages. Keying on the
    payload collapses them to one.
    """
    blocks = detection.get("blocks") or []
    if not blocks:
        return None
    parts = [
        f"{(b.get('consulate') or '').lower()}|{(b.get('month') or '').lower()}"
        f"|{','.join(str(d) for d in sorted(b.get('days') or []))}"
        for b in blocks
    ]
    return hashlib.sha1("||".join(sorted(parts)).encode()).hexdigest()


def _is_repeated_payload(state: dict, detection: dict, now: float, window: float) -> bool:
    """True when this exact availability was already announced inside *window*."""
    key = _payload_key(detection)
    if key is None:
        return False
    seen = state.setdefault("payloads", {})
    seen = {k: t for k, t in seen.items() if now - t < window}
    already = key in seen
    seen[key] = now
    state["payloads"] = seen
    return already


def _describe_blocks(blocks: list) -> list:
    """Human-readable 'NEW DELHI — September 2026: 2, 3, 4 (21 seats)' lines."""
    lines = []
    for block in blocks:
        days = ", ".join(str(d) for d in block.get("days") or [])
        line = f"{block.get('consulate', '?')} — {block.get('month') or 'date unclear'}"
        if days:
            line += f": {days}"
        if block.get("seats"):
            line += f"  ({block['seats']} seats)"
        lines.append(line)
    return lines


# ── Scoring ──────────────────────────────────────────────────────────────────

def _reputation(cfg: dict, source: str) -> int:
    src = source.lower()
    for key, value in cfg["sources"].get("reputation", {}).items():
        if key.lower() in src:
            return int(value)
    return int(cfg["sources"].get("default_reputation", 1))


# ── History ──────────────────────────────────────────────────────────────────

def _append_history(row: list[str]) -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    new_file = not os.path.exists(_HISTORY_CSV)
    with open(_HISTORY_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["timestamp", "source", "confidence", "score",
                             "consulates", "dates", "urgent", "age_minutes",
                             "stale", "seats", "message"])
        writer.writerow(row)


# ── Main entry ───────────────────────────────────────────────────────────────

def process_message(cfg: dict, source: str, text: str) -> bool:
    """Classify one message from any source; alert if it looks like a slot
    opening. Returns True if an alert (siren or quiet push) was fired."""
    detection = slot_parser.classify(text, cfg["filter"])
    if detection is None:
        return False
    if detection["confidence"] == "medium" and not cfg["filter"].get("alert_on_uncertain", True):
        logger.info(f"[{source}] medium-confidence message skipped: {text[:120]!r}")
        return False

    now = time.time()
    state = _load_state()
    _prune_state(state, now)
    if _is_repeated_template(state, text, now):
        _save_state(state)
        logger.info(f"[{source}] repeated ad template suppressed: {text[:120]!r}")
        return False
    payload_window = cfg["filter"].get("payload_repeat_window_seconds", _PAYLOAD_WINDOW)
    if _is_repeated_payload(state, detection, now, payload_window):
        _save_state(state)
        logger.info(f"[{source}] same availability re-announced, suppressed: "
                    f"{'; '.join(_describe_blocks(detection['blocks']))}")
        return False

    reputation = _reputation(cfg, source)
    score = _CONFIDENCE_POINTS[detection["confidence"]] + reputation
    consulates = detection["consulates"] or ["unknown"]
    dates = detection["dates"]
    stale = bool(detection.get("stale"))
    age = detection.get("age_minutes")

    cooldown = cfg["alerts"].get("cooldown_seconds", 180)
    off_cooldown = any(now - state["last_alarm"].get(c, 0) > cooldown for c in consulates)
    # A stale report never rings the siren: by the time a delayed channel
    # relays it the slot is gone, and a false alarm at 3am costs more than the
    # information is worth. It still goes out as a quiet, labelled push.
    urgent = off_cooldown and not stale and score >= int(cfg["sources"].get("min_urgent_score", 3))
    if urgent:
        for c in consulates:
            state["last_alarm"][c] = now
    _save_state(state)

    place = ", ".join(consulates).upper()
    title = f"VISA SLOT: {place}" if place != "UNKNOWN" else "VISA SLOT (location unclear)"
    if stale:
        title = f"[STALE {age:.0f}m] {title}"
    body_lines = [
        f"Source: {source}",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)",
        f"Confidence: {detection['confidence']} | source trust {reputation}/3 | score {score}",
    ]
    if age is not None:
        freshness = "STALE — likely already booked" if stale else "fresh"
        body_lines.append(f"Reported {age:.0f} min ago by the source bot ({freshness})")
    blocks = _describe_blocks(detection.get("blocks") or [])
    if blocks:
        body_lines.append("SLOTS:")
        body_lines.extend(f"  {line}" for line in blocks)
    elif dates:
        body_lines.append(f"Dates mentioned: {', '.join(dates)}")
    if detection["visa_types"]:
        body_lines.append(f"Visa types: {', '.join(detection['visa_types']).upper()}")
    if detection.get("attempt"):
        body_lines.append(f"Applies to: {detection['attempt']} applicants")
    body_lines.append("ACTION: log in to the portal and book manually NOW "
                      "(steps: BOOKING_PLAYBOOK.md)")
    body_lines.append("")
    body_lines.append(text[:600])
    body = "\n".join(body_lines)

    logger.info(
        f"ALERT [{source}] {title} confidence={detection['confidence']} "
        f"score={score} urgent={urgent}"
    )
    alerts.fire(cfg["alerts"], title, body, urgent=urgent)
    _append_history([
        datetime.now().isoformat(timespec="seconds"),
        source,
        detection["confidence"],
        str(score),
        ";".join(consulates),
        ";".join(dates),
        str(urgent),
        f"{age:.1f}" if age is not None else "",
        str(stale),
        str(detection.get("seats") or ""),
        text[:600].replace("\n", " "),
    ])
    return True
