# Quickstart (send this to whoever is running it)

Goal: your phone rings a loud alarm the moment an F1 slot opening is posted,
with the consulate and dates, and tapping the alert opens the booking portal.

## On your phone (2 min)

1. Install the **ntfy** app (Play Store / App Store).
2. Keep it handy — the setup below gives you a topic name to subscribe to.

## On the laptop (10 min)

Needs Python 3.10+ installed (`python3 --version` to check).

```bash
git clone -b visaSlot-auto https://github.com/aakashchamola/automated-scraping.git
cd automated-scraping/visa_slot_monitor
./start.sh          # Windows: start.bat
```

The wizard walks you through the rest:

- **Pick real-time mode (option 1).** The slot-tracker channels have their
  public web preview disabled, so the no-login fallback can't see them —
  real-time mode is the only mode that works for them. It asks for
  `api_id` / `api_hash` — get them free at https://my.telegram.org →
  *API development tools*. Also **join the watched channels** from your
  Telegram app.
- **Phone alerts**: it suggests a random topic name — subscribe to exactly
  that name in the ntfy app. Android: long-press the topic → notification
  settings → **max priority, loud sound, override Do Not Disturb**.
- It ends with a **test alert** — don't skip it; confirm the phone actually
  buzzes loudly before trusting the system.

First real-time run asks for your Telegram phone number + OTP once.

## Then

- Leave it running with the laptop awake (disable sleep in power settings).
- You'll get a quiet "monitor online" push, a daily heartbeat, and automatic
  restarts if anything crashes — if the heartbeat ever stops coming, the
  laptop is off or offline.
- Both of you can subscribe to the same ntfy topic on separate phones.
- When the alarm fires: tap the notification → portal opens → log in and
  book. Keep DS-160 number, passport and fee receipt details saved nearby;
  the alert only wins the race if booking takes under ~2 minutes.

## Also do (independent of this system)

- Install the [CheckVisaSlots](https://checkvisaslots.com/) Chrome extension
  in the browser you'll book from — crowdsourced availability without
  logging into the portal repeatedly.
