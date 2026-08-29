"""
slot_parser.py — Classify Telegram messages as visa-slot alerts.

Two paths, tried in order:

1. **Structured bot updates.** The notifier channels are fed by scraping bots
   that post a fixed template ("UPDATE from BOT / Consulate : X / <Month
   Year>: / 2,3,4 / Time-stamp( ... IST)"). Those are the only messages that
   carry real slot data, so they get a dedicated parser that pulls the
   consulate, the actual day numbers, the seat counts and — critically — the
   bot's own observation timestamp, which is what tells us whether the alert
   is fresh enough to be worth racing for.

2. **Heuristic free text.** Everything else (member chatter, other channels)
   falls back to keyword + structure matching with the broker-ad kills.

classify() returns None for irrelevant messages, or a dict describing the
detection. The caller decides whether medium confidence messages trigger the
alarm (config: filter.alert_on_uncertain).
"""

import re
from datetime import datetime, timedelta, timezone

# Indian/intl phone number in the text — near-certain broker ad
# (lookarounds, not \b: must also match digit runs glued to words)
_PHONE = re.compile(r"(?<!\d)\d{10,13}(?!\d)")

# Broker ads advertise every visa category at once; genuine slot
# notifications are specific. 3+ families mentioned = advertisement.
_VISA_FAMILIES = (
    ("f1", "f-1", "f2", "f1/f2"),
    ("b1", "b2", "b1/b2"),
    ("h1b", "h4", "h1b/h4"),
    ("l1", "l2", "l1/l2"),
    ("j1", "j2", "j1/j2"),
    ("m1", "m2", "m1/m2"),
)

# A @handle or URL is addressing, not content. "@f1_visa_slots_updatesonly"
# literally contains "f1" and "slot", so an admin's "don't share your
# credentials" housekeeping post scored as a high-confidence slot opening.
_HANDLE_OR_URL = re.compile(r"(?:@[\w_]+|https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)

# dd/mm, dd-mm-yyyy, dd.mm.yy ...
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b")
# "14 Aug", "Aug 14", "14th September"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_WORD_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})[a-z]*|(?:{_MONTHS})[a-z]*\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
    re.IGNORECASE,
)

# ── Structured "UPDATE from BOT" template ────────────────────────────────────

_BOT_HEADER = re.compile(r"update\s+from\s+bot", re.IGNORECASE)
# "Time-stamp( 2026-08-23 17:24:29 IST)" — the bot's OWN observation time.
# This line must never be mined for slot dates; it is the alert's age.
_TIMESTAMP_LINE = re.compile(
    r"time[-\s]*stamp\s*\(\s*(\d{4})-(\d{2})-(\d{2})[\sT]+(\d{1,2}):(\d{2}):(\d{2})\s*([A-Z]{2,4})?\s*\)",
    re.IGNORECASE,
)
_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z \-]*?)\s*:\s*(.+?)\s*$")
_CONSULATE_LINE = re.compile(r"^\s*consulate\s*:\s*(.+?)\s*$", re.IGNORECASE)
_MONTH_LINE = re.compile(
    r"^\s*((?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{4})\s*:\s*(.*)$",
    re.IGNORECASE,
)
_DAYS_LINE = re.compile(r"^\s*\d{1,2}(?:\s*,\s*\d{1,2})*\s*$")
_SLOTS_COUNT = re.compile(r"(\d+)\s*slots?\s+on\s+(\d{1,2})", re.IGNORECASE)

# Timezones the bot templates use. Everything is normalised to UTC so the
# freshness maths is not at the mercy of the host machine's locale.
_TZ_OFFSETS = {"IST": timedelta(hours=5, minutes=30), "UTC": timedelta(0), "GMT": timedelta(0)}


def _find_all(text: str, needles: list) -> list:
    return [n for n in needles if n in text]


def strip_timestamp_line(text: str) -> str:
    """Remove the bot's ``Time-stamp( ... )`` line.

    The template's own timestamp is a date, so a naive date scan reports it as
    a slot date — the alert then says "Dates mentioned: 08-23" while the real
    slot days ("September 2026: 2,3,4...") never make it into the alert.
    """
    return _TIMESTAMP_LINE.sub(" ", text)


def extract_dates(text: str) -> list:
    """Free-text date mentions, excluding the bot's own timestamp line."""
    cleaned = strip_timestamp_line(text)
    dates = _NUMERIC_DATE.findall(cleaned) + _WORD_DATE.findall(cleaned)
    return list(dict.fromkeys(dates))  # dedupe, keep order


def parse_timestamp(text: str):
    """The bot's observation time as an aware UTC datetime, or None."""
    m = _TIMESTAMP_LINE.search(text)
    if not m:
        return None
    year, month, day, hour, minute, second, tz = m.groups()
    try:
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None
    offset = _TZ_OFFSETS.get((tz or "IST").upper(), _TZ_OFFSETS["IST"])
    return (naive - offset).replace(tzinfo=timezone.utc)


def parse_bot_update(text: str):
    """Parse the structured notifier-bot template.

    Returns ``None`` when the text is not that template, else a dict with the
    header fields and one entry per ``Consulate:`` / ``<Month Year>:`` block::

        {"bot_id": "7039", "visa_type": "F-1", "attempt": "Fresher",
         "observed_at": datetime(...UTC), "age_minutes": 3.2,
         "blocks": [{"consulate": "NEW DELHI", "month": "September 2026",
                     "days": [2, 3, 4], "seats": 21}]}
    """
    if not text or not _BOT_HEADER.search(text):
        return None

    header = {}
    blocks = []
    current = None
    pending_seats = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"-", "=", " "}:
            continue

        cons = _CONSULATE_LINE.match(line)
        if cons:
            current = {"consulate": cons.group(1).strip(), "month": None, "days": [], "seats": None}
            blocks.append(current)
            pending_seats = False
            continue

        month = _MONTH_LINE.match(line)
        if month and current is not None:
            # A month heading opens a new block for the same consulate when the
            # current block already has one (bots list several months per post).
            if current["month"] is not None:
                current = {"consulate": current["consulate"], "month": None, "days": [], "seats": None}
                blocks.append(current)
            current["month"] = month.group(1).strip()
            trailing = month.group(2).strip()
            if _DAYS_LINE.match(trailing):
                current["days"] = [int(d) for d in re.findall(r"\d{1,2}", trailing)]
            pending_seats = False
            continue

        seats = _SLOTS_COUNT.search(line)
        if seats and current is not None:
            current["seats"] = int(seats.group(1))
            pending_seats = False
            continue

        if re.match(r"^\s*number\s+of\s+slots\s*:?\s*$", line, re.IGNORECASE):
            pending_seats = True
            continue

        if _DAYS_LINE.match(line) and current is not None:
            if pending_seats and current["days"]:
                # A bare number right after "Number of Slots :" is a seat count
                current["seats"] = int(line.split(",")[0])
            else:
                current["days"].extend(int(d) for d in re.findall(r"\d{1,2}", line))
            pending_seats = False
            continue

        if _TIMESTAMP_LINE.search(line):
            continue

        field = _FIELD.match(line)
        if field and current is None:
            header[field.group(1).strip().lower().replace(" ", "_")] = field.group(2).strip()

    blocks = [b for b in blocks if b["days"] or b["month"]]
    if not blocks:
        return None

    observed_at = parse_timestamp(text)
    age_minutes = None
    if observed_at is not None:
        age_minutes = (datetime.now(timezone.utc) - observed_at).total_seconds() / 60.0

    bot_id = header.get("botid") or header.get("bot_id") or ""
    return {
        "bot_id": re.sub(r"[^0-9]", "", bot_id) or None,
        "page": header.get("page"),
        "attempt": header.get("attempt"),
        "profile": header.get("profile"),
        "visa_type": header.get("visa_type"),
        "observed_at": observed_at,
        "age_minutes": age_minutes,
        "blocks": blocks,
    }


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _matches_watchlist(value: str, watchlist: list) -> bool:
    """True when *value* matches any entry of *watchlist* (empty = match all)."""
    if not watchlist:
        return True
    target = _normalise(value)
    if not target:
        return False
    return any(_normalise(w) and _normalise(w) in target for w in watchlist)


def _classify_bot_update(text: str, filter_cfg: dict):
    """Detection dict for a structured bot update, or None to ignore it."""
    parsed = parse_bot_update(text)
    if parsed is None:
        return None

    watch_consulates = filter_cfg.get("watch_consulates") or filter_cfg.get("consulates", [])
    watch_visa_types = filter_cfg.get("watch_visa_types", [])

    # Only keep blocks for consulates we actually care about. A Karachi or
    # Islamabad post is a real slot opening — just not one this applicant can
    # book — and firing the siren for it is pure alarm fatigue.
    relevant = [b for b in parsed["blocks"] if _matches_watchlist(b["consulate"], watch_consulates)]
    if not relevant:
        return None
    if not _matches_watchlist(parsed.get("visa_type") or "", watch_visa_types):
        return None

    max_age = filter_cfg.get("max_alert_age_minutes", 0) or 0
    age = parsed["age_minutes"]
    stale = bool(max_age) and age is not None and age > max_age

    days = []
    for block in relevant:
        month = (block["month"] or "").split()[0][:3] if block["month"] else ""
        for day in block["days"]:
            days.append(f"{month} {day}".strip())

    return {
        "confidence": "high",
        "format": "bot_update",
        "consulates": sorted({b["consulate"].lower() for b in relevant}),
        "visa_types": [parsed["visa_type"]] if parsed.get("visa_type") else [],
        "dates": list(dict.fromkeys(days)),
        "slot_keywords": ["structured bot update"],
        "blocks": relevant,
        "bot_id": parsed["bot_id"],
        "attempt": parsed.get("attempt"),
        "seats": sum(b["seats"] for b in relevant if b["seats"]) or None,
        "observed_at": parsed["observed_at"],
        "age_minutes": age,
        "stale": stale,
    }


def classify(text: str, filter_cfg: dict):
    """Return detection info if the message looks like a slot opening, else None."""
    if not text:
        return None

    structured = _classify_bot_update(text, filter_cfg)
    if structured is not None:
        return structured
    if _BOT_HEADER.search(text):
        # Recognised the template but nothing relevant survived filtering —
        # don't let the free-text path re-admit it as a vague keyword hit.
        return None

    # Match against prose only — see _HANDLE_OR_URL.
    lowered = _HANDLE_OR_URL.sub(" ", text).lower()

    if _find_all(lowered, filter_cfg.get("block_keywords", [])):
        return None
    if _PHONE.search(lowered.replace(" ", "")):
        return None
    families = sum(1 for fam in _VISA_FAMILIES if any(v in lowered for v in fam))
    if families >= 3:
        return None

    slot_hits = _find_all(lowered, filter_cfg.get("slot_keywords", []))
    visa_hits = _find_all(lowered, filter_cfg.get("visa_keywords", []))
    consulate_hits = _find_all(lowered, filter_cfg.get("consulates", []))
    dates = extract_dates(_HANDLE_OR_URL.sub(" ", text))

    if not slot_hits:
        return None

    # A slot keyword plus corroboration (place, visa type or a date) is high
    # confidence; a slot keyword alone is medium.
    if consulate_hits or visa_hits or dates:
        confidence = "high"
    else:
        confidence = "medium"

    # "new delhi" also matches "delhi" — collapse to the more specific hit
    if "new delhi" in consulate_hits and "delhi" in consulate_hits:
        consulate_hits.remove("delhi")

    return {
        "confidence": confidence,
        "format": "free_text",
        "consulates": consulate_hits,
        "visa_types": visa_hits,
        "dates": dates,
        "slot_keywords": slot_hits,
        "blocks": [],
        "seats": None,
        "observed_at": None,
        "age_minutes": None,
        "stale": False,
    }
