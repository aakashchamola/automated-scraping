"""The agent that runs the pipeline on someone's own machine.

The website cannot start a process on a laptop and the laptop cannot accept an
incoming connection, so neither calls the other: both talk to the project's
sheet. The dashboard appends a row, the agent claims it. Everything here is
about that exchange behaving when the interesting things happen — a run fails,
a cancellation arrives mid-run, the network drops, someone presses Ctrl-C.

The subprocess is real. Mocking it would leave the one part most likely to be
wrong — that output is captured, exit codes are read, and a signal actually
stops it — untested.
"""

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_module                                    # noqa: E402
import remote_store                                             # noqa: E402

PASSWORD = "right-password"


class Queue(BaseHTTPRequestHandler):
    """A stand-in for the project's Runs tab."""

    runs = []
    updates = []
    claims = 0
    cancel_after = None          # updates before a cancellation is reported
    fail_times = 0

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"ok": True, "settingsRows": [["Group", "Setting", "Value"]],
                    "keywords": [], "existingLinkHashes": [], "companyLinkedIn": {},
                    "jobsWorksheet": "Jobs"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("password") != PASSWORD:
            return self._send({"ok": False, "error": "no project matched that password"})
        if Queue.fail_times > 0:
            Queue.fail_times -= 1
            self.send_response(503)
            self.end_headers()
            return

        if body["action"] == "claimRun":
            Queue.claims += 1
            queued = [r for r in Queue.runs if r["status"] == "queued"]
            if not queued:
                return self._send({"ok": True, "run": None, "reaped": 0})
            run = queued[0]
            run["status"] = "running"
            run["claimed_by"] = body.get("agent", "")
            return self._send({"ok": True, "run": dict(run), "reaped": 0})

        if body["action"] == "updateRun":
            Queue.updates.append(body)
            for run in Queue.runs:
                if run["id"] == body["id"]:
                    if body.get("status"):
                        run["status"] = body["status"]
            cancelling = (Queue.cancel_after is not None
                          and len(Queue.updates) >= Queue.cancel_after)
            return self._send({"ok": True, "updated": body["id"],
                               "cancelRequested": cancelling})

        self._send({"ok": True})


class AgentTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Queue)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/exec"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Queue.runs = []
        Queue.updates = []
        Queue.claims = 0
        Queue.cancel_after = None
        Queue.fail_times = 0
        remote_store.BACKOFF_SEC = 0
        self.agent = agent_module.Agent(
            self.url, PASSWORD, "test-machine", "config.yaml", poll_sec=1)
        # The work itself is a script written per test, so what the pipeline
        # would do is stood in for by something with a known exit code and
        # known output.
        self.scripts = {}

    def queue(self, mode, run_id="r1"):
        Queue.runs.append({"id": run_id, "mode": mode, "status": "queued",
                           "requested_at": "", "requested_by": "dashboard",
                           "claimed_by": "", "started_at": "", "finished_at": "",
                           "exit_code": "", "summary": ""})

    def use_script(self, mode, body):
        """Point a mode at an inline python program instead of the real module."""
        agent_module.MODES[mode] = ["-c", body]
        self.addCleanup(agent_module.MODES.pop, mode, None)

    def statuses(self):
        return [u.get("status") for u in Queue.updates if u.get("status")]

    def last_summary(self):
        summaries = [u["summary"] for u in Queue.updates if u.get("summary")]
        return summaries[-1] if summaries else ""

    # ── Claiming ──────────────────────────────────────────────────────────────

    def test_an_empty_queue_runs_nothing(self):
        self.assertFalse(self.agent.poll_once())
        self.assertEqual(Queue.updates, [])

    def test_polling_is_the_heartbeat(self):
        """It must not be possible to look online while not asking for work."""
        self.agent.poll_once()
        self.assertEqual(Queue.claims, 1)

    def test_a_mode_this_agent_does_not_know_fails_the_run(self):
        # Rather than sitting queued forever, looking like the machine is off.
        self.queue("mode-from-the-future")
        self.agent.poll_once()
        self.assertEqual(self.statuses()[-1], "failed")
        self.assertIn("older than the dashboard", self.last_summary())

    # ── Running ───────────────────────────────────────────────────────────────

    def test_a_successful_run_is_reported_done_with_its_output(self):
        self.use_script("scrape-only", "print('scraped 12 jobs')")
        self.queue("scrape-only")
        self.assertTrue(self.agent.poll_once())
        self.assertEqual(self.statuses()[-1], "done")
        self.assertIn("scraped 12 jobs", self.last_summary())

    def test_a_failing_run_is_reported_failed_with_its_exit_code(self):
        self.use_script("scrape-only",
                        "import sys; print('boom'); sys.exit(3)")
        self.queue("scrape-only")
        self.agent.poll_once()
        self.assertEqual(self.statuses()[-1], "failed")
        self.assertEqual([u["exitCode"] for u in Queue.updates
                          if u.get("exitCode") is not None][-1], 3)
        self.assertIn("boom", self.last_summary())

    def test_a_crash_is_a_failure_not_a_silence(self):
        """stderr has to be captured too, or a traceback reports as 'exit 1'."""
        self.use_script("scrape-only", "raise RuntimeError('the sheet is gone')")
        self.queue("scrape-only")
        self.agent.poll_once()
        self.assertEqual(self.statuses()[-1], "failed")
        self.assertIn("the sheet is gone", self.last_summary())

    def test_the_project_password_reaches_the_run(self):
        """The pipeline needs it too — it is how the run reads the sheet."""
        self.use_script(
            "scrape-only",
            "import os; print('URL' if os.environ.get('SETTINGS_WEB_APP_URL') "
            "else 'NO URL'); print('PW' if os.environ.get('PROJECT_PASSWORD') "
            "else 'NO PW')")
        self.queue("scrape-only")
        self.agent.poll_once()
        self.assertIn("URL", self.last_summary())
        self.assertIn("PW", self.last_summary())
        self.assertNotIn(PASSWORD, self.last_summary(),
                         "the password must not be echoed into the summary")

    def test_the_run_leaves_the_repository_alone(self):
        """CI rewrites config.yaml on a throwaway checkout; this is a real one."""
        before = open("config.yaml", "rb").read()
        self.use_script("scrape-only", "print('ok')")
        self.queue("scrape-only")
        self.agent.poll_once()
        self.assertEqual(open("config.yaml", "rb").read(), before,
                         "a run must not modify the working copy")

    def test_output_is_kept_on_this_machine_in_full(self):
        self.use_script("scrape-only",
                        "print('\\n'.join(str(n) for n in range(500)))")
        self.queue("scrape-only", run_id="r-log")
        self.agent.poll_once()
        path = os.path.join(self.agent.workdir, "logs", "agent", "r-log.log")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.assertTrue(os.path.exists(path))
        text = open(path, encoding="utf-8").read()
        self.assertIn("499", text, "the full output belongs in the log file")
        self.assertLess(len(self.last_summary()), 4000,
                        "but the summary sent to the sheet stays small")

    # ── Cancelling ────────────────────────────────────────────────────────────

    def test_a_cancellation_stops_a_run_in_progress(self):
        """The point of the exercise: a long run has to be stoppable."""
        self.use_script(
            "scrape-only",
            "import time\n"
            "for n in range(600):\n"
            "    print('working', n, flush=True)\n"
            "    time.sleep(0.05)\n")
        agent_module.PROGRESS_SEC = 0          # report on the first line
        self.addCleanup(setattr, agent_module, "PROGRESS_SEC", 20)
        Queue.cancel_after = 1
        self.queue("scrape-only")

        started = time.time()
        self.agent.poll_once()
        self.assertLess(time.time() - started, 25,
                        "it must not run to completion after being cancelled")
        self.assertEqual(self.statuses()[-1], "cancelled")

    def test_stopping_the_agent_does_not_leave_a_run_looking_alive(self):
        """A row stuck on 'running' blocks everything queued behind it."""
        self.use_script(
            "scrape-only",
            "import time\n"
            "for n in range(600):\n"
            "    print('working', n, flush=True)\n"
            "    time.sleep(0.05)\n")
        agent_module.PROGRESS_SEC = 0
        self.addCleanup(setattr, agent_module, "PROGRESS_SEC", 20)
        self.queue("scrape-only")

        threading.Timer(1.0, self.agent.stop).start()
        self.agent.poll_once()
        self.assertEqual(self.statuses()[-1], "failed")
        self.assertIn("agent was stopped", self.last_summary())

    # ── When the far side misbehaves ──────────────────────────────────────────

    def test_a_failed_report_does_not_lose_the_run(self):
        """The work is done; a network blip must not turn that into a failure."""
        self.use_script("scrape-only", "print('done the work')")
        self.queue("scrape-only")
        original = self.agent._post

        def flaky(action, **fields):
            if action == "updateRun":
                raise remote_store.RemoteStoreError("network went away")
            return original(action, **fields)

        self.agent._post = flaky
        self.agent.poll_once()          # must not raise

    def test_the_loop_survives_the_service_being_down(self):
        Queue.fail_times = 1
        remote_store.RETRIES = 1
        self.addCleanup(setattr, remote_store, "RETRIES", 3)
        self.agent.serve(once=True)     # must not raise

    def test_a_wrong_password_is_not_mistaken_for_an_empty_queue(self):
        wrong = agent_module.Agent(self.url, "not-it", "test", "config.yaml")
        with self.assertRaises(remote_store.RemoteStoreError):
            wrong.claim()


if __name__ == "__main__":
    unittest.main(verbosity=2)
