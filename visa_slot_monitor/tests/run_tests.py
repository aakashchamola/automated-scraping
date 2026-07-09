"""
Offline test suite (stdlib only, no network, no Telegram account needed).

    python tests/run_tests.py

Covers: parser verdicts on a genuine-notification corpus and a real-world
spam corpus, date/consulate extraction, dispatcher scoring, cross-process
cooldown persistence, template suppression, and config validation.
"""

import json
import os
import sys
import tempfile
import unittest

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MODULE_DIR)

import config_util   # noqa: E402
import dispatcher    # noqa: E402
import slot_parser   # noqa: E402


def load_cfg() -> dict:
    with open(os.path.join(_MODULE_DIR, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


# Modeled on the formats real notifier bots/channels use (checkvisaslots-style
# structured posts, auto-notifier bots, human "go now" posts).
GENUINE = [
    "🇺🇸 F1 Visa Slots Available!\nLocation: CHENNAI\nDates: 14 Aug 2026, 19 Aug 2026\nChecked at: 10:32 AM IST",
    "F1 (Regular): HYDERABAD VAC slots available | Earliest: 18-Aug-2026 | Seen: 2 min ago",
    "OFC Appointment slots opened - MUMBAI VAC - 21/08/2026 - F1",
    "ALERT: New Delhi consular F-1 interview calendar open for September 2026",
    "Kolkata slots released!! seeing 03-09-2026 and 04-09-2026 on the portal right now, student visa",
    "SLOT ALERT: Mumbai F-1 interview dates available 12/08/2026 to 20/08/2026",
]

# Verbatim (trimmed) from the real July 2026 export of an F1 slots group.
SPAM = [
    "USA Visa Appointments Available Now Expert support for booking visa appointments and DS-160 "
    "form completion. Visa types handled: F1/F2 Regular or Refused H1B/H4 Regular or Dropbox "
    "B1/B2 Dropbox or Regular Contact on WhatsApp: 918341022486",
    "Visa slot available Low Cost Charges very Genuine F1, F2 . (Fresher, Refused) B1, B2 "
    "( Dropbox, Regular ) H1B,H4 (Regular, Refused ) All Visa slots booking",
    "2026 Visa Slots booking available for July, August and September dates available now if "
    "interested inbox me now for your fast booking for all location and categories",
    "Anyone looking for _July_ August _September _October_ Dates Reach out for fast "
    "confirmation/No advance payment",
    "If you're trying to book your visa appointment slot, I recommend reaching out to @Jaffrin_Aftab .",
    "Hi admin, Please remove this sai Rahul he took of 15000 rs from me and he didn't book the slot",
    "💹 #WLD/USDT x20 Margin mode - CROSS Entry - current market price Stop Loss - $0.3444",
    "Dates available for July August September October 2026 All categories H1B/H4 B1B2 F1F2 M1M2 L1L2 "
    "Confirmation within an hour",
    "Good morning everyone",
    "My interview is on 14 Aug at Delhi, what documents do I need?",
]


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.filter_cfg = load_cfg()["filter"]

    def test_genuine_messages_detected(self):
        for msg in GENUINE:
            with self.subTest(msg=msg[:60]):
                det = slot_parser.classify(msg, self.filter_cfg)
                self.assertIsNotNone(det, f"genuine message missed: {msg[:80]}")
                self.assertEqual(det["confidence"], "high")

    def test_spam_rejected(self):
        for msg in SPAM:
            with self.subTest(msg=msg[:60]):
                self.assertIsNone(slot_parser.classify(msg, self.filter_cfg),
                                  f"spam leaked: {msg[:80]}")

    def test_extraction(self):
        det = slot_parser.classify(GENUINE[0], self.filter_cfg)
        self.assertEqual(det["consulates"], ["chennai"])
        self.assertIn("14 Aug", det["dates"])
        det = slot_parser.classify(GENUINE[3], self.filter_cfg)
        self.assertEqual(det["consulates"], ["new delhi"])

    def test_phone_number_blocks(self):
        msg = "F1 slots open at Chennai for Aug 14! call 98765 43210"
        self.assertIsNone(slot_parser.classify(msg, self.filter_cfg))

    def test_many_visa_families_blocks(self):
        msg = "slots open F1/F2 B1/B2 H1B/H4 L1/L2 all available at Chennai"
        self.assertIsNone(slot_parser.classify(msg, self.filter_cfg))


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()
        self.tmp = tempfile.mkdtemp()
        # isolate persistent state + history
        dispatcher._STATE_PATH = os.path.join(self.tmp, "state.json")
        dispatcher._HISTORY_CSV = os.path.join(self.tmp, "history.csv")
        dispatcher._LOG_DIR = self.tmp
        self.fired = []
        dispatcher.alerts.fire = (
            lambda a, title, body, urgent=True: self.fired.append((title, body, urgent)))

    def test_reputation_scoring(self):
        self.assertEqual(dispatcher._reputation(self.cfg, "t.me/AllIndiaVisaAutoSlotNotifier"), 3)
        self.assertEqual(dispatcher._reputation(self.cfg, "reddit.com/r/f1visa"), 2)
        self.assertEqual(dispatcher._reputation(self.cfg, "t.me/some_unknown_channel"), 1)

    def test_cross_source_dedupe_and_body(self):
        msg1 = "F1 slots opened at HYDERABAD for Aug 14"
        msg2 = "Hyderabad F1 slots available now, 14 Aug seen on portal"
        self.assertTrue(dispatcher.process_message(self.cfg, "t.me/AllIndiaVisaAutoSlotNotifier", msg1))
        self.assertTrue(dispatcher.process_message(self.cfg, "reddit.com/r/f1visa", msg2))
        (t1, b1, u1), (t2, b2, u2) = self.fired
        self.assertTrue(u1, "first sighting must siren")
        self.assertFalse(u2, "second source within cooldown must be quiet")
        self.assertIn("HYDERABAD", t1)
        for needle in ("Source:", "Time:", "Confidence:", "ACTION:", "Aug 14"):
            self.assertIn(needle, b1)

    def test_cooldown_survives_process_restart(self):
        msg = "F1 slots opened at MUMBAI for Aug 20"
        dispatcher.process_message(self.cfg, "t.me/AllIndiaVisaAutoSlotNotifier", msg)
        # simulate a fresh process: state must come from disk, not memory
        state = dispatcher._load_state()
        self.assertIn("mumbai", state["last_alarm"])

    def test_template_suppression(self):
        ad = "F1 slots opened at KOLKATA for Aug 25"
        self.assertTrue(dispatcher.process_message(self.cfg, "t.me/a", ad))
        self.assertTrue(dispatcher.process_message(self.cfg, "t.me/b", ad))
        self.assertFalse(dispatcher.process_message(self.cfg, "t.me/c", ad),
                         "3rd identical text within 24h must be suppressed")

    def test_low_trust_medium_confidence_never_sirens(self):
        cfg = load_cfg()
        cfg["filter"]["alert_on_uncertain"] = True
        # slot keyword only, unknown source -> score 1+1=2 < 3 -> quiet
        self.assertTrue(dispatcher.process_message(cfg, "t.me/randomgroup", "slots just opened!!"))
        self.assertFalse(self.fired[-1][2])


class ConfigTests(unittest.TestCase):
    def test_valid_config_loads(self):
        cfg = config_util.load_config(os.path.join(_MODULE_DIR, "config.json"))
        self.assertIn("sources", cfg)
        self.assertGreaterEqual(cfg["sources"]["min_urgent_score"], 1)

    def test_broken_config_exits(self):
        bad = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write('{"telegram": {}}')
        with self.assertRaises(SystemExit):
            config_util.load_config(bad)

    def test_missing_creds_exits_when_required(self):
        cfg_path = os.path.join(tempfile.mkdtemp(), "config.json")
        cfg = load_cfg()
        cfg["telegram"]["api_id"] = ""
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(SystemExit):
            config_util.load_config(cfg_path, require_telegram_creds=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
