# Booking Playbook — win the two minutes after the alarm

The alert only wins the race if the human side is rehearsed. Slots vanish
in minutes; treat this like a fire drill you've practiced.

## Pre-flight (do once, today)

1. **Dedicated browser profile** just for booking (Chrome → profile
   "VISA"). In it, keep exactly two pinned tabs:
   - https://www.usvisascheduling.com/ (logged in)
   - your email inbox (for OTP mails)
2. **Save the login** in that profile's password manager so it autofills.
3. Keep a note (physical + in the profile's bookmarks bar as a bookmark
   named with the info) containing: passport number, DS-160 confirmation
   number, SEVIS ID, fee receipt (MRV) number.
4. **OTP readiness**: know which channel the portal uses for you (email or
   SMS). Keep that inbox logged in; keep the phone on you.
5. Install the [CheckVisaSlots extension](https://checkvisaslots.com/) in
   this profile — it shows crowdsourced availability without you refreshing.
6. Log in to the portal **once every day or two** so the session/password
   never surprises you (normal human frequency — never automate this).
7. Phone: ntfy topic subscribed, max priority, override DND — verified with
   `python alerts.py --test`.
8. Decide preferences NOW, not during the panic: which consulates are
   acceptable, which date ranges work. Write them on the note.

## When the alarm fires

1. **Tap the notification** — it opens the portal directly. (Laptop: the
   pinned tab.)
2. Read the alert body while it loads: *which consulate, which dates*.
3. Log in (autofill), go to reschedule/booking, select the consulate from
   the alert.
4. **Take any acceptable slot immediately** — book first; you can usually
   reschedule to a better date later. A mediocre slot beats no slot.
5. OTP arrives → enter fast (inbox already open from pre-flight).
6. Screenshot the confirmation.

## Rules learned from the data

- **Never pay "agents" from Telegram groups.** The channel data contains
  their victims' complaints. Every "payment after confirmation" post is
  the same scam template.
- Don't refresh the portal in a loop while waiting — lockout risk is real;
  that's exactly what this system exists to avoid.
- If the alert was real but you missed the slot: stay logged in ~15
  minutes. Openings often come in dribbles (cancellations, batch releases).
- False alarm? Note the message text and add its phrasing to
  `filter.block_keywords`, or send it to the maintainer.
