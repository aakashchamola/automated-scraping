"""
backtest.py — Replay a Telegram chat export through the slot parser.

Feed it the export of a slot-tracker channel (Telegram Desktop → channel →
⋮ → Export chat history → JSON or HTML) and it shows exactly what the live
monitor would have alerted on, what it would have blocked as spam, and —
most importantly — near-misses: messages that mention a consulate or date
but matched no slot keyword. Near-misses are the candidates for keyword
gaps, so tune config.json filter.* from this report.

Usage:
    python backtest.py path/to/result.json
    python backtest.py path/to/messages.html
    python backtest.py path/to/ChatExport_2026-07-31/        # whole export folder
"""

import argparse
import csv
import glob
import json
import os
import sys

import slot_parser

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_OUT_CSV = os.path.join(_MODULE_DIR, "logs", "backtest_results.csv")


# ── Export loaders ───────────────────────────────────────────────────────────

def _flatten_text(t) -> str:
    """Telegram JSON 'text' is a string or a list of strings/entity dicts."""
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in t)
    return ""


def load_json_export(path: str) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = _flatten_text(m.get("text", ""))
        if text.strip():
            out.append((m.get("date", ""), text))
    return out


def load_html_export(paths: list[str]) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            soup = BeautifulSoup(fh.read(), "html.parser")
        for div in soup.select("div.message"):
            text_el = div.select_one("div.text")
            if not text_el:
                continue
            date_el = div.select_one("div.date")
            date = date_el.get("title", "") if date_el else ""
            text = text_el.get_text(" ", strip=True)
            if text:
                out.append((date, text))
    return out


def collect(path: str) -> list[tuple[str, str]]:
    if os.path.isdir(path):
        result_json = os.path.join(path, "result.json")
        if os.path.exists(result_json):
            return load_json_export(result_json)
        htmls = sorted(glob.glob(os.path.join(path, "messages*.html")))
        if htmls:
            return load_html_export(htmls)
        sys.exit(f"No result.json or messages*.html inside {path}")
    if path.endswith(".json"):
        return load_json_export(path)
    if path.endswith((".html", ".htm")):
        return load_html_export([path])
    sys.exit("Expected a Telegram export: result.json, messages.html, or the export folder")


# ── Classification with reasons ──────────────────────────────────────────────

def verdict_for(text: str, filter_cfg: dict) -> tuple[str, dict | None]:
    detection = slot_parser.classify(text, filter_cfg)
    if detection:
        return f"alert-{detection['confidence']}", detection
    lowered = text.lower()
    if any(k in lowered for k in filter_cfg.get("block_keywords", [])):
        return "blocked-spam", None
    consulate = any(c in lowered for c in filter_cfg.get("consulates", []))
    if consulate or slot_parser.extract_dates(text):
        return "near-miss", None  # place/date mentioned but no slot keyword
    return "ignored", None


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a Telegram export through the slot parser")
    ap.add_argument("export", help="result.json / messages.html / export folder")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    ap.add_argument("--show", type=int, default=15, help="how many examples to print per bucket")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        filter_cfg = json.load(fh)["filter"]

    messages = collect(args.export)
    if not messages:
        sys.exit("Export contained no text messages.")

    buckets: dict[str, list] = {"alert-high": [], "alert-medium": [], "blocked-spam": [], "near-miss": [], "ignored": []}
    os.makedirs(os.path.dirname(_OUT_CSV), exist_ok=True)
    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "verdict", "consulates", "dates", "text"])
        for date, text in messages:
            verdict, detection = verdict_for(text, filter_cfg)
            buckets[verdict].append((date, text, detection))
            writer.writerow([
                date,
                verdict,
                ";".join(detection["consulates"]) if detection else "",
                ";".join(detection["dates"]) if detection else "",
                text[:300].replace("\n", " "),
            ])

    total = len(messages)
    print(f"\n{total} messages replayed from {args.export}\n")
    print(f"{'verdict':14} {'count':>6}   meaning")
    print(f"{'alert-high':14} {len(buckets['alert-high']):>6}   would have fired the SIREN")
    print(f"{'alert-medium':14} {len(buckets['alert-medium']):>6}   fires only if alert_on_uncertain=true")
    print(f"{'blocked-spam':14} {len(buckets['blocked-spam']):>6}   killed by block_keywords")
    print(f"{'near-miss':14} {len(buckets['near-miss']):>6}   consulate/date seen but NO slot keyword — check these!")
    print(f"{'ignored':14} {len(buckets['ignored']):>6}   irrelevant chatter")

    for bucket, header in (
        ("alert-high", "WOULD-ALERT (high)"),
        ("alert-medium", "WOULD-ALERT (medium)"),
        ("near-miss", "NEAR-MISSES — real slot posts here mean slot_keywords needs additions"),
    ):
        rows = buckets[bucket]
        if not rows:
            continue
        print(f"\n── {header} — showing {min(args.show, len(rows))} of {len(rows)} " + "─" * 20)
        for date, text, detection in rows[: args.show]:
            extra = ""
            if detection:
                extra = f"  [{', '.join(detection['consulates']) or 'place?'} | {', '.join(detection['dates']) or 'no date'}]"
            print(f"  {date[:16]:16}{extra}  {text[:110].replace(chr(10), ' ')}")

    print(f"\nFull per-message results: {_OUT_CSV}")


if __name__ == "__main__":
    main()
