"""Offline tests for the static-site export and its encryption."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import encrypt_snapshot                       # noqa: E402
import export_snapshot                        # noqa: E402


class EncryptionTests(unittest.TestCase):
    """The published files sit on a world-readable URL, so the password has to
    be the decryption key, not a comparison the viewer can skip."""

    PASSWORD = "correct-horse-battery"

    def test_round_trip(self):
        plaintext = json.dumps({"rows": [{"Company": "Natera"}]}).encode()
        payload = encrypt_snapshot.encrypt_bytes(plaintext, self.PASSWORD)
        self.assertEqual(
            encrypt_snapshot.decrypt_payload(payload, self.PASSWORD), plaintext)

    def test_wrong_password_cannot_decrypt(self):
        payload = encrypt_snapshot.encrypt_bytes(b"secret rows", self.PASSWORD)
        with self.assertRaises(Exception):
            encrypt_snapshot.decrypt_payload(payload, "wrong-password")

    def test_plaintext_never_appears_in_the_payload(self):
        payload = encrypt_snapshot.encrypt_bytes(b"Thermo Fisher Scientific", self.PASSWORD)
        self.assertNotIn("Thermo", json.dumps(payload))

    def test_salt_and_iv_differ_every_time(self):
        a = encrypt_snapshot.encrypt_bytes(b"same input", self.PASSWORD)
        b = encrypt_snapshot.encrypt_bytes(b"same input", self.PASSWORD)
        self.assertNotEqual(a["kdf"]["salt"], b["kdf"]["salt"])
        self.assertNotEqual(a["cipher"]["iv"], b["cipher"]["iv"])
        self.assertNotEqual(a["data"], b["data"])

    def test_parameters_are_the_ones_webcrypto_implements(self):
        # The browser derives the key with no crypto library, so these must not
        # drift from what site/app.js passes to crypto.subtle.
        payload = encrypt_snapshot.encrypt_bytes(b"x", self.PASSWORD)
        self.assertEqual(payload["kdf"]["name"], "PBKDF2")
        self.assertEqual(payload["kdf"]["hash"], "SHA-256")
        self.assertEqual(payload["cipher"]["name"], "AES-GCM")
        self.assertGreaterEqual(payload["kdf"]["iterations"], 100_000)

    def test_one_publish_shares_a_salt_but_never_an_iv(self):
        # A shared salt is what lets the browser derive the key once and cache
        # it as a non-extractable CryptoKey instead of keeping the password.
        # A shared IV would break AES-GCM, so those must still differ.
        tmp = tempfile.mkdtemp()
        for name in ("Jobs", "Companies", "index"):
            with open(os.path.join(tmp, f"{name}.json"), "w") as fh:
                json.dump({"worksheet": name}, fh)
        sys.argv = ["encrypt_snapshot.py", "--in", tmp, "--out", tmp,
                    "--password", self.PASSWORD]
        encrypt_snapshot.main()

        payloads = []
        for name in ("Jobs", "Companies", "index"):
            with open(os.path.join(tmp, f"{name}.enc.json")) as fh:
                payloads.append(json.load(fh))
        salts = {p["kdf"]["salt"] for p in payloads}
        ivs = {p["cipher"]["iv"] for p in payloads}
        self.assertEqual(len(salts), 1, "all files in a publish share one salt")
        self.assertEqual(len(ivs), 3, "every file needs its own IV")
        for payload in payloads:
            encrypt_snapshot.decrypt_payload(payload, self.PASSWORD)

    def test_separate_publishes_do_not_reuse_a_salt(self):
        # Rotating the password must invalidate every cached key, which only
        # holds if a fresh publish derives a different key.
        salts = set()
        for _ in range(2):
            tmp = tempfile.mkdtemp()
            with open(os.path.join(tmp, "a.json"), "w") as fh:
                json.dump({}, fh)
            sys.argv = ["encrypt_snapshot.py", "--in", tmp, "--out", tmp,
                        "--password", self.PASSWORD]
            encrypt_snapshot.main()
            with open(os.path.join(tmp, "a.enc.json")) as fh:
                salts.add(json.load(fh)["kdf"]["salt"])
        self.assertEqual(len(salts), 2)

    def test_cli_removes_the_cleartext_it_encrypted(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "Jobs.json")
        with open(src, "w") as fh:
            json.dump({"rows": [1, 2, 3]}, fh)
        sys.argv = ["encrypt_snapshot.py", "--in", tmp, "--out", tmp,
                    "--password", self.PASSWORD]
        encrypt_snapshot.main()
        self.assertFalse(os.path.exists(src), "cleartext must not survive")
        self.assertTrue(os.path.exists(os.path.join(tmp, "Jobs.enc.json")))


class ExportTargetTests(unittest.TestCase):
    def test_worksheets_come_from_config_without_duplicates(self):
        cfg = {
            "google_sheets": {
                "jobs_worksheet": "Jobs_Test",
                "enrichment_output_worksheet": "CompaniesTest",
                "company_sheet": {"worksheet": "CompaniesTest"},   # deliberate repeat
            },
            "scraping": {"keywords_source": {"worksheet": "Keywords"}},
        }
        self.assertEqual(export_snapshot._worksheets(cfg),
                         ["Jobs_Test", "CompaniesTest", "Keywords"])

    def test_missing_names_are_skipped(self):
        cfg = {"google_sheets": {"jobs_worksheet": "Jobs"}, "scraping": {}}
        self.assertEqual(export_snapshot._worksheets(cfg), ["Jobs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
