"""
alerts.py — Alert delivery: phone push (ntfy), local siren, desktop notification.

The siren WAV is generated on first use with the stdlib (no audio assets in
the repo) and played with whatever player the OS has. Sound playback runs in
a background thread so the Telethon event loop is never blocked.

Test the full alert path end to end:
    python alerts.py --test
"""

import json
import logging
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import wave

import requests

logger = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_SIREN_PATH = os.path.join(_MODULE_DIR, "siren.wav")


# ── Phone push via ntfy ──────────────────────────────────────────────────────

def send_ntfy(ntfy_cfg: dict, title: str, message: str, urgent: bool = True) -> bool:
    topic = ntfy_cfg.get("topic", "").strip()
    if not topic:
        logger.warning("ntfy enabled but no topic configured — skipping push")
        return False
    url = f"{ntfy_cfg.get('server', 'https://ntfy.sh').rstrip('/')}/{topic}"
    headers = {
        "Title": title,
        "Priority": ntfy_cfg.get("priority", "urgent") if urgent else "default",
        "Tags": "rotating_light,us" if urgent else "information_source",
    }
    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error(f"ntfy push failed: {exc}")
        return False


# ── Local siren ──────────────────────────────────────────────────────────────

def _ensure_siren_wav() -> str:
    if os.path.exists(_SIREN_PATH):
        return _SIREN_PATH
    sample_rate = 22050
    seconds = 3.0
    frames = []
    for i in range(int(sample_rate * seconds)):
        t = i / sample_rate
        freq = 880 if int(t * 4) % 2 == 0 else 660  # two-tone siren
        frames.append(struct.pack("<h", int(28000 * math.sin(2 * math.pi * freq * t))))
    with wave.open(_SIREN_PATH, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))
    return _SIREN_PATH


def _play_once(path: str) -> None:
    system = platform.system()
    if system == "Windows":
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
        return
    if system == "Darwin":
        subprocess.run(["afplay", path], check=False)
        return
    for player in ("paplay", "aplay", "ffplay"):
        exe = shutil.which(player)
        if exe:
            cmd = [exe, path]
            if player == "ffplay":
                cmd = [exe, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
            subprocess.run(cmd, check=False)
            return
    # Last resort: terminal bell
    sys.stdout.write("\a" * 5)
    sys.stdout.flush()


def play_siren(repeat: int = 4) -> None:
    def _run():
        try:
            path = _ensure_siren_wav()
            for _ in range(max(1, repeat)):
                _play_once(path)
        except Exception as exc:
            logger.error(f"siren playback failed: {exc}")

    threading.Thread(target=_run, daemon=True).start()


# ── Desktop notification (best effort) ───────────────────────────────────────

def desktop_notify(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, message], check=False)
        elif system == "Darwin":
            script = f'display notification "{message[:200]}" with title "{title}" sound name "Sosumi"'
            subprocess.run(["osascript", "-e", script], check=False)
    except Exception as exc:
        logger.debug(f"desktop notification failed: {exc}")


# ── Combined dispatch ────────────────────────────────────────────────────────

def fire(alerts_cfg: dict, title: str, message: str, urgent: bool = True) -> None:
    """Fire every enabled alert channel. `urgent=False` (cooldown repeats)
    sends a quiet push and skips the siren."""
    ntfy_cfg = alerts_cfg.get("ntfy", {})
    if ntfy_cfg.get("enabled"):
        send_ntfy(ntfy_cfg, title, message, urgent=urgent)
    if urgent:
        sound_cfg = alerts_cfg.get("local_sound", {})
        if sound_cfg.get("enabled"):
            play_siren(sound_cfg.get("repeat", 4))
        if alerts_cfg.get("desktop_notification", {}).get("enabled"):
            desktop_notify(title, message)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--test" in sys.argv:
        with open(os.path.join(_MODULE_DIR, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        fire(
            cfg["alerts"],
            "TEST: F1 slot alert",
            "This is a test alert from visa_slot_monitor. If your phone buzzed and the laptop siren played, you are all set.",
        )
        print("Test alert fired. Waiting for siren to finish...")
        import time
        time.sleep(15)
    else:
        print("Usage: python alerts.py --test")
