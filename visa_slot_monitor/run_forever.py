"""
run_forever.py — Supervisor: runs one or more sources simultaneously,
keeps them alive 24/7.

Each source runs as its own child process and is restarted independently
whenever it dies, with backoff. Restarts send a quiet phone push; repeated
fast crashes escalate to an urgent one, because a dead monitor means
missed slots. A source that fails immediately on its very first run
(bad credentials, pending OTP) is dropped with the error left on screen
instead of being restarted in a loop.

Usage:
    python run_forever.py                          # targets from config (monitoring.preferred_entry)
    python run_forever.py --target monitor,reddit  # explicit list
    python run_forever.py --target all
"""

import argparse
import logging
import os
import subprocess
import sys
import time

import alerts
import config_util

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_TARGETS = {
    "monitor": "monitor.py",
    "poller": "web_preview_poller.py",
    "reddit": "reddit_source.py",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s supervisor: %(message)s")
logger = logging.getLogger("supervisor")


def main() -> None:
    ap = argparse.ArgumentParser(description="Keep the visa slot sources running")
    ap.add_argument("--target", default=None,
                    help="comma-separated sources (monitor,poller,reddit) or 'all'")
    ap.add_argument("--config", default=os.path.join(_MODULE_DIR, "config.json"))
    args = ap.parse_args()

    cfg = config_util.load_config(args.config)
    raw = args.target or cfg.get("monitoring", {}).get("preferred_entry", "monitor")
    names = sorted(_TARGETS) if raw.strip() == "all" else [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [n for n in names if n not in _TARGETS]
    if unknown:
        sys.exit(f"Unknown target(s) {unknown}; valid: {sorted(_TARGETS)}")

    def spawn(name: str) -> subprocess.Popen:
        logger.info(f"starting {name} ({_TARGETS[name]})")
        return subprocess.Popen(
            [sys.executable, os.path.join(_MODULE_DIR, _TARGETS[name]), "--config", args.config]
        )

    state = {
        n: {"proc": spawn(n), "started": time.time(), "backoff": 5,
            "fast_crashes": 0, "ever_stable": False, "restart_at": 0.0, "dead": False}
        for n in names
    }

    try:
        while True:
            time.sleep(5)
            now = time.time()
            for name, st in state.items():
                if st["dead"]:
                    continue
                if st["proc"] is None:  # waiting out the backoff
                    if now >= st["restart_at"]:
                        st["proc"] = spawn(name)
                        st["started"] = now
                    continue
                if st["proc"].poll() is None:
                    if now - st["started"] > 600:
                        st["ever_stable"], st["backoff"], st["fast_crashes"] = True, 5, 0
                    continue

                rc = st["proc"].returncode
                uptime = now - st["started"]
                st["proc"] = None

                if not st["ever_stable"] and uptime < 30 and rc != 0:
                    logger.error(
                        f"{name} exited with code {rc} after {uptime:.0f}s on first run — "
                        "looks like a setup problem, not restarting it. Fix and start again."
                    )
                    st["dead"] = True
                    continue

                if uptime <= 600:
                    st["fast_crashes"] += 1
                    st["backoff"] = min(st["backoff"] * 2, 300)
                urgent = st["fast_crashes"] >= 3
                alerts.fire(
                    cfg["alerts"],
                    f"Visa source '{name}' restarted" if not urgent else f"Visa source '{name}' KEEPS CRASHING",
                    f"{name} exited (code {rc}) after {uptime / 60:.1f} min. "
                    f"Restarting in {st['backoff']}s." + (" Needs attention!" if urgent else ""),
                    urgent=urgent,
                )
                logger.warning(f"{name} exited code={rc}; restart in {st['backoff']}s")
                st["restart_at"] = now + st["backoff"]

            if all(st["dead"] for st in state.values()):
                sys.exit("All sources failed on startup — nothing left to supervise.")
    except KeyboardInterrupt:
        logger.info("stopping all sources")
        for st in state.values():
            if st["proc"] is not None and st["proc"].poll() is None:
                st["proc"].terminate()


if __name__ == "__main__":
    main()
