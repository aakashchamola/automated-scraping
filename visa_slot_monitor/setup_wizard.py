"""
setup_wizard.py — Interactive first-time setup. Run once:

    python setup_wizard.py

Walks through: choosing real-time vs no-login mode, Telegram API
credentials, phone alert (ntfy) topic, channel list — then writes
config.json and fires a test alert so you know the buzzer works.
"""

import json
import os
import secrets
import string
import sys

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_MODULE_DIR, "config.json")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print("\nAborted (no interactive input available).")
        sys.exit(1)
    return value or default


def main() -> None:
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    print("=" * 62)
    print(" F1 VISA SLOT MONITOR — SETUP")
    print("=" * 62)

    # ── Mode ─────────────────────────────────────────────────────────
    print(
        "\nHow should Telegram be watched?\n"
        "  1) Real-time (recommended) — instant alerts; needs free API\n"
        "     credentials from https://my.telegram.org + one OTP login\n"
        "  2) No-login — polls public channel preview pages every ~45s.\n"
        "     WARNING: the default slot-tracker channels have the web\n"
        "     preview DISABLED, so option 2 cannot see them. Only pick 2\n"
        "     if you've verified https://t.me/s/<channel> shows messages."
    )
    mode = ""
    while mode not in ("1", "2"):
        mode = ask("Choose 1 or 2", "1")
    # Reddit runs alongside either mode as a free redundant source
    cfg.setdefault("monitoring", {})["preferred_entry"] = (
        "monitor,reddit" if mode == "1" else "poller,reddit"
    )

    if mode == "1":
        print(
            "\nGet credentials at https://my.telegram.org → log in →\n"
            "'API development tools' → create any app → copy the two values."
        )
        while True:
            api_id = ask("api_id (numbers only)", str(cfg["telegram"].get("api_id", "")))
            if api_id.isdigit():
                break
            print("api_id must be numeric.")
        cfg["telegram"]["api_id"] = api_id
        cfg["telegram"]["api_hash"] = ask("api_hash", cfg["telegram"].get("api_hash", ""))
        print(
            "\nIMPORTANT: also JOIN each channel below from your Telegram app,\n"
            "otherwise real-time updates for it won't arrive."
        )

    # ── Phone alerts ─────────────────────────────────────────────────
    alphabet = string.ascii_lowercase + string.digits
    generated = "f1slots-" + "".join(secrets.choice(alphabet) for _ in range(10))
    print(
        "\nPhone alerts use the free ntfy app (no account needed):\n"
        "  1. Install 'ntfy' from the Play Store / App Store\n"
        "  2. In the app: Subscribe to topic → enter the topic name below\n"
        "  3. Android: long-press the topic → notification settings →\n"
        "     max priority + loud sound + override Do Not Disturb\n"
        "Anyone who knows the topic name can read the alerts — treat it\n"
        "like a password. Everyone hunting can subscribe to the same topic."
    )
    topic = ask("ntfy topic", cfg["alerts"]["ntfy"].get("topic") or generated)
    cfg["alerts"]["ntfy"]["topic"] = topic

    # ── Channels ─────────────────────────────────────────────────────
    print("\nChannels currently watched:")
    for ch in cfg["telegram"]["channels"]:
        print(f"  - t.me/{ch}")
    extra = ask("Add more channels (comma-separated usernames, Enter to skip)")
    if extra:
        for ch in extra.split(","):
            ch = ch.strip().lstrip("@").replace("https://t.me/", "").replace("t.me/", "")
            if ch and ch not in cfg["telegram"]["channels"]:
                cfg["telegram"]["channels"].append(ch)

    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=4)
        fh.write("\n")
    print(f"\nSaved {_CONFIG_PATH}")

    # ── Test the alarm path ──────────────────────────────────────────
    if ask("Fire a test alert now? (y/n)", "y").lower().startswith("y"):
        import time
        import alerts
        alerts.fire(
            cfg["alerts"],
            "TEST: visa slot alert",
            f"Setup complete. Phone subscribed to '{topic}' should be buzzing "
            "and the laptop siren playing. If not, re-check the ntfy app setup.",
        )
        print("Test alert sent — check your phone and speakers.")
        time.sleep(8)

    entry = "python run_forever.py"
    print(
        "\nDone. Start the monitor with:\n"
        f"    {entry}\n"
        "(or just run start.sh / start.bat — it does everything)"
    )


if __name__ == "__main__":
    main()
