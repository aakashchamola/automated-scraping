"""
run_forever.py — Supervisor: keeps the monitor alive 24/7.

Runs monitor.py (or web_preview_poller.py) as a child process and restarts
it whenever it dies, with backoff. Each restart sends a quiet phone push;
repeated fast crashes escalate to an urgent one, because a dead monitor
means missed slots.

Usage:
    python run_forever.py                    # target from config (monitoring.preferred_entry)
    python run_forever.py --target poller    # force the no-login poller
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

import alerts

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_TARGETS = {"monitor": "monitor.py", "poller": "web_preview_poller.py"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s supervisor: %(message)s")
logger = logging.getLogger("supervisor")


def main() -> None:
    ap = argparse.ArgumentParser(description="Keep the visa slot monitor running")
    ap.add_argument("--target", choices=sorted(_TARGETS), default=None)
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    target = args.target or cfg.get("monitoring", {}).get("preferred_entry", "monitor")
    script = os.path.join(_MODULE_DIR, _TARGETS[target])

    backoff = 5
    fast_crashes = 0
    while True:
        started = time.time()
        logger.info(f"starting {target} ({script})")
        try:
            proc = subprocess.run([sys.executable, script, "--config", args.config])
        except KeyboardInterrupt:
            logger.info("stopped by user")
            return
        uptime = time.time() - started

        # First run exiting immediately is a setup problem (bad credentials,
        # pending OTP login) — restarting in a loop would just spam; bail out
        # so the error above stays on screen.
        if uptime < 30 and fast_crashes == 0 and proc.returncode != 0:
            logger.error(
                f"{target} exited with code {proc.returncode} after {uptime:.0f}s. "
                "Fix the error above, then start again."
            )
            return

        if uptime > 600:
            backoff, fast_crashes = 5, 0
        else:
            fast_crashes += 1
            backoff = min(backoff * 2, 300)

        urgent = fast_crashes >= 3
        alerts.fire(
            cfg["alerts"],
            "Visa monitor restarted" if not urgent else "Visa monitor KEEPS CRASHING",
            f"{target} exited (code {proc.returncode}) after {uptime / 60:.1f} min. "
            f"Restarting in {backoff}s." + (" Needs attention!" if urgent else ""),
            urgent=urgent,
        )
        logger.warning(f"{target} exited code={proc.returncode}; restarting in {backoff}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            logger.info("stopped by user")
            return


if __name__ == "__main__":
    main()
