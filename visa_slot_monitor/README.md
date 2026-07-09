# F1 Visa Slot Monitor

Rings a loud alarm on your phone **and** laptop the moment a US visa
interview slot opening is reported in the public Telegram channels that track
them, telling you which consulate and which dates — so you can log in and
book immediately.

**This system alerts; a human books.** It deliberately does not touch the
official scheduling portal (usvisascheduling.com): auto-booking bots violate
the portal's terms, trip its anti-bot defenses, and risk getting the
applicant's account locked — the last thing you want mid slot-hunt. The
reliable play is instant notification + fast manual booking.

---

## How it works

```
Telegram channels (public slot-tracker groups)
        │
        ├── monitor.py              ← real-time push via your Telegram account (REQUIRED
        │                             for the default channels — see note below)
        └── web_preview_poller.py   ← polls t.me/s/<channel>, no account needed; only
                                      works for channels with web preview enabled
        │
        ▼
slot_parser.py    keyword + consulate + date detection, spam filtering
        │
        ▼
dispatcher.py     dedupe/cooldown, history CSV
        │
        ▼
alerts.py         ├── ntfy push → phone (alarm-grade notification)
                  ├── siren on the laptop speakers
                  └── desktop notification
```

Everything is configured in `config.json`. The pre-loaded channels are the
ones from our research; add more under `telegram.channels` (username only,
without `t.me/`).

Around the core sits the reliability layer: `run_forever.py` supervises the
process (auto-restart with backoff, crash pushes to your phone), and
`monitor.py` sends a startup push, a daily heartbeat, and a warning if all
channels go silent for hours (usually a dropped connection). A dead monitor
you still trust is the failure mode this exists to prevent.

---

## Fast path

```bash
cd visa_slot_monitor
./start.sh        # Windows: start.bat
```

That creates a venv, installs dependencies, runs the interactive
`setup_wizard.py` on first use (Telegram credentials, phone alert topic,
test alert), then starts the supervised monitor. The manual equivalent is
below.

Docs map: [QUICKSTART.md](QUICKSTART.md) (operator setup) ·
[RUN_AND_TEST.md](RUN_AND_TEST.md) (run/test/troubleshoot + example
messages) · [DEPLOYMENT.md](DEPLOYMENT.md) (laptop/Termux/Pi/Docker/systemd) ·
[BOOKING_PLAYBOOK.md](BOOKING_PLAYBOOK.md) (human response drill) ·
[PROBLEM_AND_PROGRESS.md](PROBLEM_AND_PROGRESS.md) (project brief).
`config.sample.json` is a pristine reference copy of the config.

---

## Setup (~15 minutes, manual)

### 1. Python side

```bash
cd visa_slot_monitor
pip install -r requirements.txt
```

### 2. Phone alerts (ntfy — free, no signup)

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)).
2. In the app, subscribe to a topic with a long random name, e.g.
   `f1-slots-parbonee-x7k2m9` (anyone who knows the name can see the
   alerts, so make it unguessable).
3. Put that topic name in `config.json` → `alerts.ntfy.topic`.
4. **Android, to make it alarm-grade:** app Settings → enable *Instant
   delivery*; then long-press the subscribed topic → notification settings →
   max priority, loud sound, override Do Not Disturb.
5. Everyone hunting (you + Parbonee) can subscribe to the same topic on
   their own phones.

### 3. Telegram side (for `monitor.py`, the real-time path)

1. Log into https://my.telegram.org → *API development tools* → create an
   app → copy **api_id** and **api_hash** into `config.json`.
2. From your Telegram app, **join every channel** listed in `config.json`.
3. First run will ask for your phone number + OTP once, then stay logged in.

### 4. Test the alarm path before trusting it

```bash
python alerts.py --test     # phone should buzz loudly + laptop siren plays
```

### 5. Run

```bash
python run_forever.py             # supervised (recommended); runs ALL configured
                                  # sources simultaneously (default: monitor + reddit)
python run_forever.py --target monitor,reddit   # explicit source list
python run_forever.py --target all

# bare entry points, without the supervisor:
python monitor.py                 # Telegram real-time (primary)
python reddit_source.py           # Reddit r/f1visa + r/usvisascheduling polling
python web_preview_poller.py      # t.me/s/ polling, no account
```

Sources run as independent supervised processes and all feed the same
parser → dedupe → alarm pipeline, so adding a source never doubles the
sirens (per-consulate cooldown handles cross-source duplicates).

> **Note (verified July 2026):** the default slot-tracker channels have
> their public web preview disabled — `t.me/s/<channel>` shows only a
> join prompt. The poller therefore cannot monitor them; use the
> real-time mode. The poller remains for any additional channels that do
> keep their preview enabled, and it warns per channel when it can't see
> one.

### Tuning against real channel history

Export a channel's history from Telegram Desktop (channel → ⋮ → *Export
chat history* → JSON or HTML) and replay it:

```bash
python backtest.py path/to/result.json
```

It reports what would have sirened, what was blocked as spam, and
**near-misses** (consulate/date mentioned but no slot keyword matched) —
tune `filter.*` in config.json from that report. Full per-message verdicts
land in `logs/backtest_results.csv`.

Keep the laptop awake (disable sleep) or run it on any always-on box — a
Raspberry Pi or the cheapest VPS works; the phone still gets the push via
ntfy. `web_preview_poller.py` even runs on an Android phone inside
[Termux](https://termux.dev) if a laptop can't stay on.

---

## Tuning detection

All in `config.json` → `filter`:

| Key | Meaning |
|---|---|
| `slot_keywords` | at least one must appear for a message to be considered |
| `consulates` | place names to extract (Delhi/Mumbai/Chennai/Hyderabad/Kolkata) |
| `visa_keywords` | F1/B1... used to raise confidence and shown in the alert |
| `block_keywords` | kill-list for ads/spam ("dm me", "paid service", ...) |
| `alert_on_uncertain` | `true` = also alarm on slot-keyword-only messages. Start `true` (missing a slot is worse than a false alarm); flip to `false` if a channel is noisy |

`alerts.cooldown_seconds` (default 180): when the same consulate is posted
across several groups within the window, you get one siren; the repeats
arrive as quiet pushes so your phone isn't a continuous alarm. Cooldown and
template-suppression state persist in `logs/dispatcher_state.json`, shared
by all source processes — one slot seen by Telegram AND Reddit is still one
siren, and restarts don't re-alarm.

**Scoring** (`sources` in config): each alert's score = parser confidence
(high 2 / medium 1) + source trust (`sources.reputation`, 1–3, matched by
substring against the source name). Siren requires score ≥
`sources.min_urgent_score` (default 3); below that it's a quiet push. Rank
new channels by adding them to the reputation map.

**Extra alert channels** (both optional, off by default): `alerts.telegram_bot`
mirrors alerts to a Telegram chat via a @BotFather bot; `alerts.email` sends
an email backup via SMTP (e.g. Gmail app password), by default for urgent
alerts only.

Every fired alert is appended to `logs/alerts_history.csv` — after a few
days this doubles as a dataset of *when* slots tend to open (day of week,
time of day), which is genuinely useful for the fastest-finger game.

---

## Complementary tools (recommended alongside this)

- **[CheckVisaSlots](https://checkvisaslots.com/)** Chrome extension —
  crowdsourced slot screenshots from other applicants' portal checks; install
  it in the browser you'll book from. Their alert channels can also be added
  to `telegram.channels` here.
- Keep the usvisascheduling.com login **saved in the booking browser** and
  the DS-160 / fee receipt details ready — the alert is only useful if the
  booking itself takes under 2 minutes.

## Security notes

- `visa_monitor.session` (created by Telethon) **is your Telegram login** —
  it's gitignored, never share or commit it.
- The ntfy topic name is the only "password" on your alerts — keep it random.
