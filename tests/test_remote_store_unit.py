"""Running the pipeline with a project password instead of Google credentials.

The service-account key can read and write every spreadsheet it has ever been
shared with, and there is no way to scope it to one project — so it cannot be
handed to anyone else. This is the path that does not need it: the Apps Script
supplies the inputs and takes back the results, and the machine doing the work
holds only the project's password.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_store                                            # noqa: E402
import storage                                                 # noqa: E402

PASSWORD = "right-password"
SETTINGS = [
    ["Group", "Setting", "Value", "Type", "Options", "Description"],
    ["Sheets", "google_sheets.jobs_worksheet", "Jobs_Test", "text", "", ""],
]
INPUTS = {
    "ok": True, "project": "main", "settingsRows": SETTINGS,
    "keywords": ["microbiologist", "research associate"],
    "jobsWorksheet": "Jobs_Test", "jobsHeader": ["Company", "Role", "Job Link"],
    "existingLinkHashes": [remote_store.link_hash("https://example.com/job/1")],
    "linkHashChars": 12,
    "companyLinkedIn": {"acme": "https://linkedin.com/company/acme"},
}


class Stub(BaseHTTPRequestHandler):
    """Stands in for the Web App, and records what it was sent."""

    posts = []
    gets = 0
    fail_times = 0
    html_reply = False

    def log_message(self, *args):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self, given):
        return given == PASSWORD

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        Stub.gets += 1
        if Stub.fail_times > 0:
            Stub.fail_times -= 1
            self.send_response(503); self.end_headers(); return
        if Stub.html_reply:
            body = b"<!DOCTYPE html><html>Error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        query = parse_qs(urlparse(self.path).query)
        if not self._authorised(query.get("password", [""])[0]):
            return self._send({"ok": False, "error": "no project matched that password"})
        self._send(dict(INPUTS))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._authorised(body.get("password")):
            return self._send({"ok": False, "error": "no project matched that password"})
        Stub.posts.append(body)
        self._send({"ok": True, "added": len(body.get("rows") or []), "duplicates": 0,
                    "worksheet": body.get("worksheet"), "total": 0})


class RemoteStoreTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Stub)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/exec"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.posts = []
        Stub.gets = 0
        Stub.fail_times = 0
        Stub.html_reply = False
        remote_store.BACKOFF_SEC = 0            # no real waiting in a test
        self.store = remote_store.RemoteSheetsStore(self.url, PASSWORD)

    # ── Reading ───────────────────────────────────────────────────────────────

    def test_it_is_enabled_only_with_both_halves(self):
        self.assertTrue(self.store.is_enabled())
        self.assertFalse(remote_store.RemoteSheetsStore(self.url, "").is_enabled())
        self.assertFalse(remote_store.RemoteSheetsStore("", PASSWORD).is_enabled())

    def test_keywords_come_from_the_service(self):
        self.assertEqual(self.store.load_column_values("Search Term", "Keywords"),
                         ["microbiologist", "research associate"])

    def test_the_settings_tab_arrives_as_raw_rows(self):
        # settings_sheet.read_overrides parses these itself, so the shape has to
        # match what a real worksheet read returns.
        rows = self.store.load_all_rows("Settings")
        self.assertEqual(rows[0][:3], ["Group", "Setting", "Value"])
        self.assertEqual(rows[1][1], "google_sheets.jobs_worksheet")

    def test_the_company_map_comes_across(self):
        self.assertEqual(self.store.load_company_linkedin_map(),
                         {"acme": "https://linkedin.com/company/acme"})

    def test_existing_rows_are_hashes_not_jobs(self):
        # A machine running the pipeline is never handed the jobs already
        # collected — only enough to skip them.
        frame = self.store.load_existing()
        self.assertEqual(list(frame.columns), storage.OUTPUT_COLUMNS)
        self.assertEqual(len(frame), 1)
        self.assertNotIn("example.com", str(frame["Job Link"].iloc[0]))

    def test_inputs_are_fetched_once_per_run(self):
        self.store.load_column_values("Search Term", "Keywords")
        self.store.load_company_linkedin_map()
        self.store.load_existing()
        self.assertEqual(Stub.gets, 1, "a run wants one stable view, not three")

    def test_a_tab_it_cannot_serve_says_so(self):
        # Silently returning nothing would look like an empty sheet.
        with self.assertRaises(remote_store.RemoteStoreError):
            self.store.load_all_rows("Jobs_Test")
        with self.assertRaises(remote_store.RemoteStoreError):
            self.store.load_column_values("Company", "Company")

    # ── Writing ───────────────────────────────────────────────────────────────

    def frame(self, count, start=100):
        return pd.DataFrame([
            {"Company": f"C{n}", "Role": "Analyst",
             "Job Link": f"https://example.com/job/{n}"}
            for n in range(start, start + count)])

    def test_rows_are_sent_to_the_service(self):
        self.store.append_rows(self.frame(2))
        self.assertEqual(len(Stub.posts), 1)
        self.assertEqual(Stub.posts[0]["action"], "appendJobs")
        self.assertEqual(Stub.posts[0]["worksheet"], "Jobs_Test")
        self.assertEqual(len(Stub.posts[0]["rows"]), 2)

    def test_rows_already_in_the_sheet_are_not_resent(self):
        rows = pd.DataFrame([
            {"Company": "Old", "Role": "x", "Job Link": "https://example.com/job/1"},
            {"Company": "New", "Role": "y", "Job Link": "https://example.com/job/2"},
        ])
        self.store.append_rows(rows)
        sent = [r["Company"] for r in Stub.posts[0]["rows"]]
        self.assertEqual(sent, ["New"])

    def test_nothing_is_sent_when_there_is_nothing_new(self):
        rows = pd.DataFrame([
            {"Company": "Old", "Role": "x", "Job Link": "https://example.com/job/1"}])
        self.store.append_rows(rows)
        self.assertEqual(Stub.posts, [])

    def test_an_empty_frame_sends_nothing(self):
        self.store.append_rows(pd.DataFrame())
        self.store.append_rows(None)
        self.assertEqual(Stub.posts, [])

    def test_large_runs_are_batched(self):
        # Apps Script stops one execution at six minutes, so a thousand rows
        # cannot go in a single request.
        self.store.append_rows(self.frame(600))
        self.assertEqual(len(Stub.posts), 3)
        self.assertEqual(sum(len(p["rows"]) for p in Stub.posts), 600)
        self.assertTrue(all(len(p["rows"]) <= remote_store.APPEND_BATCH
                            for p in Stub.posts))

    def test_the_password_never_appears_in_a_row(self):
        self.store.append_rows(self.frame(1))
        self.assertNotIn(PASSWORD, json.dumps(Stub.posts[0]["rows"]))

    # ── Failing ───────────────────────────────────────────────────────────────

    def test_a_wrong_password_is_refused(self):
        wrong = remote_store.RemoteSheetsStore(self.url, "not-it")
        with self.assertRaises(remote_store.RemoteStoreError) as caught:
            wrong.load_column_values("Search Term", "Keywords")
        self.assertIn("no project matched", str(caught.exception))

    def test_a_transient_failure_is_retried(self):
        Stub.fail_times = 2
        self.assertEqual(self.store.load_column_values("Search Term", "Keywords"),
                         ["microbiologist", "research associate"])

    def test_it_gives_up_eventually(self):
        Stub.fail_times = 99
        with self.assertRaises(remote_store.RemoteStoreError):
            self.store.load_column_values("Search Term", "Keywords")

    def test_googles_html_error_page_is_explained(self):
        # Google answers with a web page when the deployment cannot open the
        # spreadsheet, and a JSON parse error would hide that entirely.
        Stub.html_reply = True
        with self.assertRaises(remote_store.RemoteStoreError) as caught:
            self.store.load_column_values("Search Term", "Keywords")
        self.assertIn("web page", str(caught.exception))

    def test_missing_configuration_is_named(self):
        for url, password in [("", PASSWORD), (self.url, "")]:
            with self.subTest(url=bool(url), password=bool(password)):
                store = remote_store.RemoteSheetsStore(url, password)
                with self.assertRaises(remote_store.RemoteStoreError):
                    store.load_all_rows("Settings")


class ChoosingAStore(unittest.TestCase):

    def setUp(self):
        self.saved = {k: os.environ.get(k)
                      for k in ("SETTINGS_WEB_APP_URL", "PROJECT_PASSWORD")}
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_the_owners_machine_is_unchanged(self):
        # With no password in the environment, nothing about the existing path
        # may change.
        self.assertFalse(remote_store.is_configured())
        store = remote_store.store_for({"google_sheets": {"enabled": True}})
        self.assertEqual(type(store).__name__, "GoogleSheetsStore")

    def test_a_password_switches_to_the_service(self):
        os.environ["SETTINGS_WEB_APP_URL"] = "https://example.test/exec"
        os.environ["PROJECT_PASSWORD"] = "something"
        self.assertTrue(remote_store.is_configured())
        self.assertEqual(type(remote_store.store_for({})).__name__, "RemoteSheetsStore")

    def test_half_a_configuration_is_not_enough(self):
        os.environ["SETTINGS_WEB_APP_URL"] = "https://example.test/exec"
        self.assertFalse(remote_store.is_configured())
        os.environ.pop("SETTINGS_WEB_APP_URL")
        os.environ["PROJECT_PASSWORD"] = "something"
        self.assertFalse(remote_store.is_configured())


if __name__ == "__main__":
    unittest.main()
