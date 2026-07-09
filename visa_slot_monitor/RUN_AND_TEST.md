# Run & Test Guide

Everything needed to run the system, verify it works, and understand what
it will and won't alert on. For first-time setup see [QUICKSTART.md](QUICKSTART.md);
for deployment targets see [DEPLOYMENT.md](DEPLOYMENT.md).

---

## 1. Run

```bash
cd visa_slot_monitor
./start.sh                    # everything: venv + deps + wizard (first run) + supervised sources
```

Manual equivalents:

```bash
python setup_wizard.py                    # (re)configure interactively
python run_forever.py                     # supervised; sources from config (default: monitor,reddit)
python run_forever.py --target all        # every source
python monitor.py                         # single source, unsupervised (debugging)
```

What healthy startup looks like:

```
supervisor: starting monitor (monitor.py)
supervisor: starting reddit (reddit_source.py)
visa_monitor: Watching 5 channels: AllIndiaVisaAutoSlotNotifier, ...
reddit_source: Polling r/f1visa, r/usvisascheduling every 90s
```
…and a quiet **"Visa monitor online"** push on your phone.

## 2. Test — do these before trusting the system

**a) Alarm path** (phone buzz + laptop siren + desktop popup):

```bash
python alerts.py --test
```

**b) Offline test suite** (parser, dispatcher, dedupe, config — 13 tests, no network):

```bash
python tests/run_tests.py
```

**c) End-to-end fake alert** — inject a genuine-looking message through the
real pipeline (fires REAL alerts, warn whoever is subscribed):

```bash
python -c "
import config_util, dispatcher
cfg = config_util.load_config('config.json')
dispatcher.process_message(cfg, 't.me/manual_test',
    'TEST DRILL: F1 slots open at CHENNAI for 14 Aug 2026')"
```

**d) Backtest against real channel history** (tuning loop):

```bash
# Telegram Desktop → channel → ⋮ → Export chat history → JSON or HTML
python backtest.py path/to/result.json
```

Read the `near-miss` bucket: genuine slot posts landing there mean
`filter.slot_keywords` needs additions. Per-message verdicts:
`logs/backtest_results.csv`.

**e) After a few days running:**

```bash
python stats.py     # alerts by source/consulate/hour/day — when do slots open?
```

## 3. Example messages and how the system treats them

### Genuine notifications → SIREN (high confidence)

These are the formats notifier bots/channels actually use:

| Message | Verdict |
|---|---|
| `🇺🇸 F1 Visa Slots Available! Location: CHENNAI Dates: 14 Aug 2026, 19 Aug 2026 Checked at: 10:32 AM IST` | siren — consulate + dates + F1 |
| `F1 (Regular): HYDERABAD VAC slots available \| Earliest: 18-Aug-2026 \| Seen: 2 min ago` | siren |
| `OFC Appointment slots opened - MUMBAI VAC - 21/08/2026 - F1` | siren |
| `ALERT: New Delhi consular F-1 interview calendar open for September 2026` | siren |

### Broker/scam spam → silently dropped (verbatim from real channel data)

| Message | Killed by |
|---|---|
| `USA Visa Appointments Available Now… Contact on WhatsApp: 918341022486` | phone-number rule + blocklist |
| `Visa slot available Low Cost Charges very Genuine F1,F2 B1,B2 H1B,H4 L1,L2…` | 3+ visa families = ad + blocklist |
| `Anyone looking for July August September Dates Reach out for fast confirmation/No advance payment` | blocklist ("reach out", "no advance") |
| `If you're trying to book your slot, I recommend reaching out to @Jaffrin_Aftab` | blocklist ("recommend") |
| `Hi admin, Please remove this sai Rahul he took 15000 rs from me…` | blocklist ("please remove") |
| `💹 #WLD/USDT x20 Margin mode - CROSS Stop Loss…` (crypto spam) | blocklist ("usdt", "margin mode") |
| Same ad posted a 3rd time in 24h | repeated-template suppression |

Validated on a real 996-message week-long export: **0 false sirens**, all
synthetic genuine formats detected. If a real genuine message ever slips
through as a miss, add its phrasing to `filter.slot_keywords` and send it
to us to become a test case.

### How scoring decides siren vs quiet push

```
score = confidence (high=2, medium=1) + source trust (config sources.reputation, 1–3)
siren  ⇔  score ≥ 3  AND  no siren for that consulate in the last 180s
otherwise → quiet phone push (still logged)
```

So: trusted bot channel + clear message → instant siren; vague message in a
low-trust group → quiet ping; the same slot echoed by a second source
within 3 minutes → quiet ping, not a second siren.

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| No phone push on `alerts.py --test` | ntfy app: subscribed to the *exact* topic in config? Instant delivery on? Re-run `setup_wizard.py` |
| Push arrives but silent | Android: long-press topic → notification settings → max priority, sound, override DND |
| `api_id must be numeric` / creds error | Re-run `python setup_wizard.py`; values from https://my.telegram.org |
| Monitor starts, then no channel traffic ever | Join each channel from the Telegram app with the same account you logged in with |
| `web preview is empty/disabled` warnings | Expected for the default channels — real-time mode covers them; the poller only helps for preview-enabled channels |
| Reddit 429/403 warnings | Rate-limited; it retries next cycle. Raise `reddit.interval_seconds` if persistent |
| "feed looks stale" push | Laptop offline / Telegram disconnected / kicked from channels — check `logs/` |
| Monitor died and phone said "restarted" | Normal (supervisor healed it). "KEEPS CRASHING" = look at logs |

## 5. Limitations (known, accepted)

- The system never touches usvisascheduling.com — by design. It can only be
  as fast as the public sources it watches.
- Telegram real-time mode requires one account to stay logged in (session
  file `visa_monitor.session` — private, gitignored).
- The default channels' web previews are disabled, so nothing works without
  either Telegram API credentials or different channels.
- Genuine-notification detection is tuned on synthetic formats + real spam;
  it improves as real genuine samples arrive (export a *good* channel and
  backtest it).
