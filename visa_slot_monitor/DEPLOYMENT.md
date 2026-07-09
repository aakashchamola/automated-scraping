# Deployment Guide

Same code and same `config.json` everywhere; only the "keep it running"
wrapper differs. The phone gets alerts via ntfy regardless of where the
monitor runs.

## Laptop (simplest — start here)

macOS / Linux: `./start.sh` · Windows: `start.bat`

Keep the machine awake:

- **Windows:** Settings → Power → Never sleep when plugged in.
- **macOS:** `caffeinate -i ./start.sh` (or Settings → prevent sleep on power).
- **Linux:** disable suspend in power settings.

### Auto-start on boot

**Linux (systemd)** — `sudo nano /etc/systemd/system/visa-monitor.service`:

```ini
[Unit]
Description=F1 visa slot monitor
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/automated-scraping/visa_slot_monitor
ExecStart=/path/to/automated-scraping/visa_slot_monitor/.venv/bin/python run_forever.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now visa-monitor
journalctl -u visa-monitor -f        # live logs
```

Run `python monitor.py` once interactively first (OTP prompt), then enable
the service.

**Windows:** Task Scheduler → Create Task → trigger *At log on* →
action `start.bat` → check "Run whether user is logged on or not" is OFF
(needs console on first run for OTP; after the session file exists it's fine).

**macOS:** simplest is an Automator app / Login Item that runs `start.sh`
in Terminal.

## Android phone (Termux) — when no laptop can stay on

Telethon is pure Python, so even the real-time monitor runs on a phone:

```bash
# in Termux (from F-Droid):
pkg install python git
git clone -b visaSlot-auto https://github.com/aakashchamola/automated-scraping.git
cd automated-scraping/visa_slot_monitor
pip install -r requirements.txt
python setup_wizard.py
python run_forever.py
```

Keep it alive: `termux-wake-lock` before starting, disable battery
optimization for Termux, and consider `pkg install termux-services`.
Note: the siren/desktop channels do nothing on Termux — ntfy push (possibly
to the same phone) is the alarm.

## Raspberry Pi / home server

Identical to Linux above (systemd). A Pi Zero 2 W is plenty; this is
I/O-light. Do the first interactive run over SSH for the OTP.

## Docker (VPS / NAS)

See `Dockerfile` header for build/run commands. Volumes to persist:
`config.json`, `visa_monitor.session`, and `logs/` (dispatcher dedupe
state + history live there).

## Redundancy pattern (recommended once stable)

Laptop runs `monitor,reddit`; phone (Termux) runs only `reddit` as a
cheap second leg. Both feed the same ntfy topic, and the shared
per-consulate cooldown is per-machine — so cross-machine duplicates can
double-push (quiet) but the phone still only alarms loudly once per ntfy
notification settings. Keep exactly ONE machine on Telegram real-time
mode per account (two simultaneous Telethon sessions on one account from
different IPs invites Telegram security logouts).
