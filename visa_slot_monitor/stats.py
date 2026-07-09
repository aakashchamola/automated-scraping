"""
stats.py — Summarize alert history: what fired, from where, and WHEN.

After a few days of running, the when-do-slots-open pattern (hour of day,
day of week) is genuinely useful intel for the fastest-finger game.

Usage:
    python stats.py
"""

import csv
import os
import sys
from collections import Counter
from datetime import datetime

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_HISTORY_CSV = os.path.join(_MODULE_DIR, "logs", "alerts_history.csv")


def _bar(count: int, top: int, width: int = 30) -> str:
    return "#" * max(1, int(width * count / top)) if top else ""


def _print_counter(title: str, counter: Counter) -> None:
    print(f"\n── {title} " + "─" * max(1, 40 - len(title)))
    top = max(counter.values(), default=0)
    for key, count in counter.most_common():
        print(f"  {str(key):24} {count:>4}  {_bar(count, top)}")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else _HISTORY_CSV
    if not os.path.exists(path):
        sys.exit(f"No history yet at {path} — run the monitor first.")
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("History file is empty.")

    urgent = [r for r in rows if r.get("urgent") == "True"]
    print(f"{len(rows)} alerts total, {len(urgent)} urgent (siren), "
          f"{len(rows) - len(urgent)} quiet pushes")

    _print_counter("By source", Counter(r["source"] for r in rows))
    _print_counter("By consulate", Counter(
        c for r in rows for c in r["consulates"].split(";") if c))

    hours, days = Counter(), Counter()
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except ValueError:
            continue
        hours[f"{ts.hour:02d}:00"] += 1
        days[ts.strftime("%A")] += 1
    _print_counter("By hour of day (local)", Counter(dict(sorted(hours.items()))))
    _print_counter("By day of week", days)


if __name__ == "__main__":
    main()
