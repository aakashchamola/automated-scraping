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
# Tabs the stub serves for `action=rows`, so the reading and writing surface is
# exercised against something that behaves like a sheet rather than a canned
# reply. Rows are 1-based in every request, header included.
TABS = {
    "Jobs_Test": [
        ["Company", "Role", "Job Link", "Job Status"],
        ["Acme", "Analyst", "https://example.com/job/1", "Live"],
        ["Globex", "Chemist", "https://example.com/job/2", "Dead"],
        ["Initech", "Biologist", "https://example.com/job/3", ""],
    ],
    "Company": [
        ["Company", "Career-Page", "Linkedin-Url"],
        ["Acme", "https://acme.test/careers", "https://linkedin.com/company/acme"],
    ],
}

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
    always_wrap = False
    callbacks_requested = []
    tabs = {}
    old_deployment = False

    def log_message(self, *args):
        pass

    def _send(self, obj, status=200, callback=""):
        """Answer the way the real Web App does.

        doGet wraps its reply in a callback whenever one is asked for, because
        the browser dashboard can only read it as JSONP. A program has no such
        constraint and wants plain JSON — but if it asks for a callback it gets
        one, and json.loads then fails on the wrapper. That is a real bug this
        stub used to hide by always answering plain JSON.
        """
        body = json.dumps(obj)
        if callback:
            body = f"{callback}({body});"
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type",
                         "text/javascript" if callback else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
        callback = query.get("callback", [""])[0]
        Stub.callbacks_requested.append(callback)
        if Stub.always_wrap and not callback:
            # A deployment made before doGet learned to serve plain JSON wraps
            # every reply whether one was asked for or not.
            callback = "callback"
        if not self._authorised(query.get("password", [""])[0]):
            return self._send({"ok": False, "error": "no project matched that password"},
                              callback=callback)
        if Stub.old_deployment:
            # What a deployment older than this code actually did: an
            # unrecognised action fell through to a settings read, which is a
            # valid-looking answer to a different question.
            return self._send({"ok": True, "settings": {"columns": [], "rows": []}},
                              callback=callback)
        if query.get("action", [""])[0] == "rows":
            tab = query.get("worksheet", [""])[0]
            # A tab that does not exist reads as empty, the way the Sheets-API
            # store reads a tab it just created.
            rows = [list(row) for row in Stub.tabs.get(tab, [])]
            return self._send({"ok": True, "worksheet": tab, "rows": rows},
                              callback=callback)
        self._send(dict(INPUTS), callback=callback)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._authorised(body.get("password")):
            return self._send({"ok": False, "error": "no project matched that password"})
        Stub.posts.append(body)
        if Stub.old_deployment:
            # And this is the dangerous one: nothing to update, so it changed
            # nothing and said ok.
            return self._send({"ok": True, "applied": [], "unknown": [],
                               "unchanged": []})
        action = body.get("action")
        tab = body.get("worksheet") or INPUTS["jobsWorksheet"]

        if action == "ensureColumn":
            rows = Stub.tabs.setdefault(tab, [[]])
            header = rows[0]
            name = body["header"]
            if name in header:
                return self._send({"ok": True, "position": header.index(name) + 1,
                                   "added": False, "worksheet": tab})
            header.append(name)
            return self._send({"ok": True, "position": len(header),
                               "added": True, "worksheet": tab})

        if action == "writeColumn":
            rows = Stub.tabs.setdefault(tab, [[]])
            col, start = int(body["col"]), int(body["startRow"])
            values = body["values"]
            while len(rows) < start + len(values) - 1:
                rows.append([])
            for offset, value in enumerate(values):
                row = rows[start + offset - 1]
                while len(row) < col:
                    row.append("")
                row[col - 1] = value
            return self._send({"ok": True, "written": len(values),
                               "worksheet": tab, "col": col, "startRow": start})

        if action == "deleteRows":
            rows = Stub.tabs.setdefault(tab, [[]])
            # Descending, or each deletion would shift the ones after it. The
            # stub does NOT sort: it is here to catch a caller that did not.
            deleted = 0
            for number in body["rows"]:
                if 2 <= number <= len(rows):
                    del rows[number - 1]
                    deleted += 1
            return self._send({"ok": True, "deleted": deleted, "worksheet": tab,
                               "requested": len(body["rows"])})

        if action == "replaceTab":
            Stub.tabs[tab] = [list(row) for row in body["rows"]]
            return self._send({"ok": True, "worksheet": tab,
                               "rows": len(body["rows"])})

        self._send({"ok": True, "added": len(body.get("rows") or []), "duplicates": 0,
                    "worksheet": tab, "total": 0})


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
        Stub.always_wrap = False
        Stub.callbacks_requested = []
        Stub.tabs = {name: [list(row) for row in rows]
                     for name, rows in TABS.items()}
        Stub.old_deployment = False
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

    def test_any_tab_of_this_project_can_be_read(self):
        """The validator and the enricher read whole tabs.

        Refusing them is what tied those stages to the service-account key, so
        four of the eight run modes needed it however the machine was set up.
        """
        rows = self.store.load_all_rows("Jobs_Test")
        self.assertEqual(rows[0], ["Company", "Role", "Job Link", "Job Status"])
        self.assertEqual(len(rows), 4)

    def test_a_tab_that_does_not_exist_reads_as_empty(self):
        # Matching GoogleSheetsStore, which creates the tab and reads nothing.
        self.assertEqual(self.store.load_all_rows("Nothing_Here"), [])

    def test_a_column_comes_off_a_tab_stripped_of_blanks(self):
        self.assertEqual(self.store.load_column_values("Job Status", "Jobs_Test"),
                         ["Live", "Dead"])
        self.assertEqual(self.store.load_column_values("Nope", "Jobs_Test"), [])

    def test_the_company_map_refuses_a_tab_the_project_is_not_configured_for(self):
        """Answering about the configured tab instead would be a quiet lie."""
        self.store.load_company_linkedin_map()          # the configured one
        with self.assertRaises(remote_store.RemoteStoreError) as caught:
            self.store.load_company_linkedin_map(worksheet_name="Somewhere_Else")
        self.assertIn("Somewhere_Else", str(caught.exception))

    # ── The rest of the pipeline's writes ─────────────────────────────────────

    def test_a_missing_status_column_is_added(self):
        position = self.store.ensure_column("Notes", "Jobs_Test")
        self.assertEqual(position, 5)
        self.assertEqual(self.store.ensure_column("Job Status", "Jobs_Test"), 4,
                         "a column that exists is found, not added again")

    def test_a_column_is_written_in_one_request(self):
        self.store.write_column_values(4, [["A"], ["B"], ["C"]], "Jobs_Test")
        writes = [p for p in Stub.posts if p["action"] == "writeColumn"]
        self.assertEqual(len(writes), 1, "one request, whatever the length")
        self.assertEqual([r[3] for r in Stub.tabs["Jobs_Test"][1:]], ["A", "B", "C"])

    def test_a_column_write_starts_below_the_header_by_default(self):
        self.store.write_column_values(4, [["X"]], "Jobs_Test")
        self.assertEqual(Stub.posts[-1]["startRow"], 2)
        self.assertEqual(Stub.tabs["Jobs_Test"][0][3], "Job Status",
                         "the header must survive")

    def test_a_very_long_column_is_split_but_stays_aligned(self):
        remote_store.COLUMN_BATCH = 10
        try:
            self.store.write_column_values(4, [[str(n)] for n in range(25)], "Jobs_Test")
        finally:
            remote_store.COLUMN_BATCH = 2000
        writes = [p for p in Stub.posts if p["action"] == "writeColumn"]
        self.assertEqual([w["startRow"] for w in writes], [2, 12, 22],
                         "each batch must start where the last one ended")
        written = [row[3] for row in Stub.tabs["Jobs_Test"][1:]]
        self.assertEqual(written, [str(n) for n in range(25)])

    def test_an_empty_column_write_sends_nothing(self):
        self.store.write_column_values(4, [], "Jobs_Test")
        self.assertEqual(Stub.posts, [])

    def test_rows_are_deleted_from_the_bottom_up(self):
        """Deleting row 2 renumbers row 3, so ascending would take the wrong ones."""
        self.store.delete_rows("Jobs_Test", [2, 3])
        self.assertEqual(Stub.posts[-1]["rows"], [3, 2])
        self.assertEqual([row[0] for row in Stub.tabs["Jobs_Test"]],
                         ["Company", "Initech"])

    def test_the_header_row_is_never_deleted(self):
        self.assertEqual(self.store.delete_rows("Jobs_Test", [1]), 0)
        self.assertEqual(Stub.posts, [])
        self.assertEqual(Stub.tabs["Jobs_Test"][0][0], "Company")

    def test_replacing_a_tab_leaves_only_the_new_rows(self):
        self.store.replace_tab("Jobs_Test", [["Only"], ["Me"]])
        self.assertEqual(Stub.tabs["Jobs_Test"], [["Only"], ["Me"]])

    def test_replacing_a_tab_with_nothing_is_refused(self):
        # A cleared tab is not what "replace" means, and it is unrecoverable.
        with self.assertRaises(remote_store.RemoteStoreError):
            self.store.replace_tab("Jobs_Test", [])
        self.assertEqual(len(Stub.tabs["Jobs_Test"]), 4)

    # ── A deployment older than this code ─────────────────────────────────────

    def test_a_read_an_old_deployment_cannot_serve_raises(self):
        """It answered with the Settings tab, which parses fine and is wrong.

        Trusting it meant validation saw an empty jobs tab, changed nothing,
        and reported success.
        """
        Stub.old_deployment = True
        with self.assertRaises(remote_store.RemoteStoreError) as caught:
            self.store.load_all_rows("Jobs_Test")
        self.assertIn("deploy a new version", str(caught.exception))

    def test_a_write_an_old_deployment_swallows_raises(self):
        """The worst case: ok:true over a column that was never written."""
        Stub.old_deployment = True
        for call in (
            lambda: self.store.write_column_values(4, [["A"]], "Jobs_Test"),
            lambda: self.store.ensure_column("Notes", "Jobs_Test"),
            lambda: self.store.delete_rows("Jobs_Test", [2]),
            lambda: self.store.replace_tab("Jobs_Test", [["A"]]),
        ):
            with self.subTest(call=call):
                with self.assertRaises(remote_store.RemoteStoreError) as caught:
                    call()
                self.assertIn("older than this code", str(caught.exception))

    def test_an_old_deployment_cannot_silently_break_the_scrape_either(self):
        Stub.old_deployment = True
        with self.assertRaises(remote_store.RemoteStoreError):
            self.store.load_existing()

    def test_colour_is_skipped_rather_than_failing_a_run(self):
        self.store.batch_format_rows([(2, {"red": 1}), (3, None)], 4, "Jobs_Test")
        self.store.batch_format_cells([(2, 1, {"red": 1})], "Jobs_Test")
        self.assertEqual(Stub.posts, [], "formatting needs the Sheets API")

    def test_open_worksheet_says_what_to_use_instead(self):
        with self.assertRaises(remote_store.RemoteStoreError) as caught:
            self.store.open_worksheet("Jobs_Test")
        self.assertIn("load_all_rows", str(caught.exception))

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
