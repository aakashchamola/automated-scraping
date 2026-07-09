# US F1 Visa Slot Alerting — Problem Brief & Progress Report

*Prepared 2026-07-09. Purpose: complete context for brainstorming additional
solution ideas. Read the constraints — ideas that violate them are unusable.*

---

## 1. The problem

An F1 (US student visa) applicant in India must secure a visa interview
appointment **before the first week of October 2026** to enroll in her
program. The US Embassy/Consulates in India are currently NOT releasing
interview slots in bulk. Slots appear at unpredictable moments, in tiny
batches, at any of five posts (New Delhi, Mumbai, Chennai, Hyderabad,
Kolkata), and are **booked out within minutes** of appearing. Manually
refreshing the booking portal is impractical (it also locks accounts out
for excessive checking).

**Goal:** an alerting system on laptop and/or phone that, the moment slots
open anywhere, rings a loud alarm and says *when and where* — so the
applicant can immediately log into the official portal and book manually.
Every minute of latency matters. Redundant, simultaneous detection channels
are explicitly wanted.

## 2. Hard constraints

1. **No auto-booking, no touching the official portal programmatically.**
   The India booking portal (usvisascheduling.com) has anti-bot defenses
   and ToS; automation risks locking/banning the applicant's account —
   catastrophic mid-hunt. The system is *detect + notify*; a human books.
2. Budget ≈ 0. Free tiers and self-hosted only.
3. Must run unattended 24/7 on a personal laptop (and/or phone), with
   failure alerts — a silently dead monitor is worse than none.
4. Timeline: days, not weeks. Working solution > elegant architecture.
5. India-specific: the five consulates above; portal is
   usvisascheduling.com (NOT the ais.usvisa-info.com system many GitHub
   tools target — those tools don't apply to India).

## 3. What exists in the ecosystem (researched)

- **Telegram groups/channels** posting slot updates. Provided by the
  client: AllIndiaVisaAutoSlotNotifier, f1_visa_slots_updatesonly,
  USAslotsupdates, f1visaslots, F1_Visa_Slots_Only.
- **checkvisaslots.com** — crowdsourced: users install a Chrome extension;
  when any user's portal view shows availability, screenshots/data are
  shared to all. Has its own alert channels. Website is Cloudflare-protected.
- **GitHub prior art:** telegram-f1-visa-tracker (Telethon group watcher),
  TELEGRAM_F1_VISA_NOTIFIER (Telethon + Twilio/Mailgun). Confirms the
  Telethon approach; nothing India-portal-specific that is safe to use.

## 4. What we've built so far (working, tested)

Repo: `automated-scraping`, branch `visaSlot-auto`, folder `visa_slot_monitor/`.
Python. Architecture:

```
sources (parallel, each supervised & auto-restarted):
  monitor.py            Telethon: real-time push from Telegram channels (user's own account)
  web_preview_poller.py polls t.me/s/<channel> — needs no account (many channels disable this)
  reddit_source.py      polls r/f1visa, r/usvisascheduling new posts (free JSON API)
        ↓
  slot_parser.py        detection: slot keywords + consulate names + date extraction
                        spam kills: blocklist, phone-number regex, "lists 3+ visa
                        categories = broker ad" heuristic
        ↓
  dispatcher.py         cross-source dedupe: per-consulate cooldown (one siren, quiet
                        repeats), repeated-ad-template suppression, history CSV
        ↓
  alerts.py             ntfy.sh push → phone (alarm-grade, overrides DND, tap opens
                        booking portal) + laptop siren + desktop notification
```

Plus: `run_forever.py` (multi-source supervisor, crash alerts), liveness
(startup push, daily heartbeat, "all channels silent for 8h" warning),
`setup_wizard.py` + one-command `start.sh`/`start.bat`, `backtest.py`
(replays a Telegram chat export through the parser for tuning).

## 5. Key empirical findings (from real data — important!)

We backtested a real 996-message, one-week export of a popular "F1 VISA
SLOTS" Telegram group (July 2026):

- **Zero genuine automated slot notifications.** 100% of slot-sounding
  messages were broker/agent advertisements (8 templates spammed
  repeatedly: "slots available, low cost, payment after confirmation, DM
  me"), scam complaints, and unrelated crypto-signal spam.
- Naive keyword filtering produced 74 false alarms/week; after tuning
  (ad-structure heuristics above) we're at 1 false alarm/week with
  synthetic genuine notifications still detected.
- Implication: **open discussion groups are broker swamps.** Value, if
  any, is in bot-driven notification channels (e.g. checkvisaslots' own
  channels, auto-notifier bots). Telegram alone is not enough — hence the
  hunt for more, independent, faster sources.
- The five channels' public web previews (t.me/s/...) are disabled, so
  account-less Telegram monitoring doesn't work for them.

## 6. Avenues identified but not yet built (with status)

| Avenue | Status / blocker |
|---|---|
| checkvisaslots' own Telegram alert channels | Just add channel names to config once identified (client is installing their extension) |
| checkvisaslots unofficial API (extension calls app.checkvisaslots.com with user API key) | Plausible; needs an account/key + endpoint inspection via browser devtools |
| visagrader.com (public availability tracker page) | Scrapeable in principle; markup not yet inspected |
| travel.state.gov global visa wait-times page | Official but updated ~weekly — trend data only, not slot sniping |
| X/Twitter accounts posting slot updates | API is paid; scraping is brittle; not pursued |
| Discord servers (Yocket, MS-in-US communities) | Possible via user-token or bot in shared servers; not pursued yet |
| WhatsApp groups | No safe automation path (ban risk); read-manually only |
| Email newsletters of visa consultancies | Slow; could pipe to a mail filter → push |
| Pattern analysis of when slots open (day-of-week/time-of-day) from our alert history CSV | Collector already running; analysis script pending data |

## 7. What we want ideas on (brainstorm prompts)

1. **More real-time, machine-readable sources** of India F1 slot-opening
   events that respect constraint #1 (no portal automation). What are we
   missing?
2. **Crowdsourcing angles**: ways to piggyback on other applicants'
   portal checks (like checkvisaslots does) without violating ToS.
3. **Detection quality**: better ways to separate genuine "slots are open
   NOW" posts from broker ads in adversarial, spam-heavy channels
   (current approach: keyword + structural heuristics; a tiny local LLM
   classifier is a candidate).
4. **Latency**: anything that beats Telegram-bot-channel latency.
5. **Booking-moment speed** (human side): pre-filled forms, browser
   profiles, checklists — shaving seconds after the alarm fires, without
   automating the portal itself.
6. **Phone-only operation**: the laptop may not stay on 24/7. (We have
   Termux polling as a partial answer; better ideas welcome.)
7. **Failure modes we haven't thought of** in a fastest-finger race
   (e.g. alarm fatigue, duplicate sources, timezone pitfalls, OTP/login
   session expiry on the watcher account).

*Do NOT suggest: auto-booking bots, CAPTCHA bypass, credential sharing,
paid "slot booking agents" (the data shows they're largely scams), or
anything that hammers the official portal.*
