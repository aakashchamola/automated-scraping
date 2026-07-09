"""
slot_parser.py — Classify Telegram messages as visa-slot alerts.

classify() returns None for irrelevant messages, or a dict describing the
detection: which consulates and dates were mentioned, which visa types, and
a confidence level ("high" / "medium"). The caller decides whether medium
confidence messages trigger the alarm (config: filter.alert_on_uncertain).
"""

import re

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

# dd/mm, dd-mm-yyyy, dd.mm.yy ...
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b")
# "14 Aug", "Aug 14", "14th September"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_WORD_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})[a-z]*|(?:{_MONTHS})[a-z]*\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
    re.IGNORECASE,
)


def _find_all(text: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n in text]


def extract_dates(text: str) -> list[str]:
    dates = _NUMERIC_DATE.findall(text) + _WORD_DATE.findall(text)
    return list(dict.fromkeys(dates))  # dedupe, keep order


def classify(text: str, filter_cfg: dict) -> dict | None:
    """Return detection info if the message looks like a slot opening, else None."""
    if not text:
        return None
    lowered = text.lower()

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
    dates = extract_dates(text)

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
        "consulates": consulate_hits,
        "visa_types": visa_hits,
        "dates": dates,
        "slot_keywords": slot_hits,
    }
