"""What the CI log is allowed to say.

The repository is public. A GitHub Actions run log and its artifacts are
readable by any signed-in GitHub user — measured, not assumed: a
non-collaborator account read both. The pipeline logs the rows it reads, and
the design's whole confidentiality claim is that project names stay hidden and
sheet contents reach the browser only as ciphertext. So the console is a
publishing surface, and these tests treat it as one.
"""

import logging
import os
import sys
import traceback
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger_setup                                            # noqa: E402
import agent as agent_module                                   # noqa: E402
import remote_store                                            # noqa: E402

WORKFLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "scheduled-pipeline.yml")


class ConsoleLevel(unittest.TestCase):
    """The console and the log file are not equally private."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("GITHUB_ACTIONS", "LOG_CONSOLE_LEVEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_locally_the_console_matches_the_file(self):
        # Running on your own machine must not change.
        self.assertEqual(logger_setup.console_level("INFO"), logging.INFO)
        self.assertEqual(logger_setup.console_level("DEBUG"), logging.DEBUG)

    def test_in_ci_the_console_is_raised(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(logger_setup.console_level("INFO"), logging.WARNING)
        # Even an explicitly chatty config must not spill into a public log.
        self.assertEqual(logger_setup.console_level("DEBUG"), logging.WARNING)

    def test_it_can_be_overridden_deliberately(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["LOG_CONSOLE_LEVEL"] = "info"
        self.assertEqual(logger_setup.console_level("INFO"), logging.INFO)

    def test_the_file_still_gets_everything(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        path = logger_setup.setup_logging(
            log_dir=os.path.join(os.path.dirname(__file__), "_tmp_logs"),
            level="INFO", name="privacy-test", force=True)
        try:
            logging.getLogger("probe").info("a company name would go here")
            logging.shutdown()
            with open(path, encoding="utf-8") as fh:
                written = fh.read()
            # The file is the debugging record and keeps INFO...
            self.assertIn("a company name would go here", written)
            # ...while the console handler is the one that was raised.
            streams = [h for h in logging.getLogger().handlers
                       if type(h) is logging.StreamHandler]
            self.assertTrue(streams)
            self.assertEqual(streams[0].level, logging.WARNING)
        finally:
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()
            os.remove(path)
            os.rmdir(os.path.dirname(path))


class WorkflowSaysNothingIdentifying(unittest.TestCase):
    """A regression guard for a leak that actually shipped.

    The 'Which project this run works on' step printed the project name and its
    spreadsheet id straight into the public run log.
    """

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()
        import yaml
        cls.doc = yaml.safe_load(cls.text)

    def _run_scripts(self):
        for job in self.doc["jobs"].values():
            for step in job["steps"]:
                if step.get("run"):
                    yield step.get("name", "<unnamed>"), step["run"]

    IDENTIFIERS = ('spreadsheet_id', '"name"', "'name'", "['id']", '["id"]')

    def test_nothing_prints_a_project_name_or_sheet_id(self):
        """An identifier may be REGISTERED as a mask, never emitted.

        ``print(f"::add-mask::{x}")`` is the fix, not a leak — everything the
        runner logs afterwards shows *** in its place. Any other line that both
        prints and mentions an identifier is the bug this guards against.
        """
        for name, script in self._run_scripts():
            for number, line in enumerate(script.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#') or 'print(' not in stripped:
                    continue
                if '::add-mask::' in stripped:
                    continue
                for needle in self.IDENTIFIERS:
                    with self.subTest(step=name, line=number, needle=needle):
                        self.assertNotIn(
                            needle, stripped,
                            f"{name!r} line {number} prints a project identifier "
                            "into a log any signed-in GitHub user can read:\n"
                            f"    {stripped}")

    def test_the_guard_would_catch_the_leak_that_shipped(self):
        # The exact line that was live, to prove this test has teeth.
        was_live = 'print("spreadsheet :", project["spreadsheet_id"])'
        self.assertTrue(any(n in was_live for n in self.IDENTIFIERS))
        self.assertNotIn(was_live, self.text)

    def test_identifiers_are_registered_as_masks(self):
        # Defence in depth: even a stray print elsewhere shows as ***.
        scripts = "\n".join(s for _, s in self._run_scripts())
        self.assertIn("::add-mask::", scripts)

    def test_the_cleartext_guard_reports_counts_not_paths(self):
        step = next(s for _, s in self._run_scripts()
                    if "refusing to publish" in s)
        # A path under site/data contains the project id.
        self.assertIn("-printf", step)
        self.assertNotIn("find site/data -type f | sort", step)

    def test_logs_are_not_uploaded_from_a_public_repository(self):
        upload = next(step for job in self.doc["jobs"].values()
                      for step in job["steps"]
                      if str(step.get("uses", "")).startswith("actions/upload-artifact")
                      and "logs" in str(step.get("with", {}).get("path", "")))
        self.assertIn("github.event.repository.private", upload.get("if", ""),
                      "the log artifact is downloadable by any signed-in GitHub "
                      "user while the repository is public")

    def _runs_python(self, job) -> bool:
        return any("python" in str(step.get("run", "")) or
                   "python" in str(step.get("uses", ""))
                   for step in job.get("steps", []))

    def test_every_job_that_runs_python_raises_the_console_level(self):
        """The pipeline logs project names; this log is world-readable.

        Asked of the jobs that actually run it rather than of all of them —
        publishing stopped exporting anything when the dashboard began reading
        the sheet directly, so it runs no Python and has no such output. A job
        that prints nothing does not need to be told to print less.
        """
        checked = 0
        for name, job in self.doc["jobs"].items():
            if not self._runs_python(job):
                continue
            checked += 1
            with self.subTest(job=name):
                self.assertEqual((job.get("env") or {}).get("LOG_CONSOLE_LEVEL"),
                                 "warning")
        self.assertTrue(checked, "no job runs Python — has the workflow changed shape?")

    def test_a_job_that_runs_no_python_names_no_project(self):
        """The exemption above has to be earned, not assumed.

        Its own steps could still echo a project name — the directories under
        site/data are named for projects — so what it prints is checked too.
        """
        for name, job in self.doc["jobs"].items():
            if self._runs_python(job):
                continue
            for step in job.get("steps", []):
                script = str(step.get("run", ""))
                with self.subTest(job=name, step=step.get("name", "")):
                    self.assertNotIn("-maxdepth 1 -type d -print", script)
                    self.assertNotIn("ls site/data", script)
                    self.assertNotIn("find site/data -type f | sort", script)


class ThePasswordNeverLeavesInAMessage(unittest.TestCase):
    """The one secret a teammate's machine holds must not be quotable.

    Every read sends the password as a query parameter — a Web App's doGet has
    no other way to be given one — and requests names the URL it tried in the
    text of anything it raises. So an ordinary DNS blip produced an error
    message with the password in it, and that message went three places that
    outlive it: the agent's log file, the Runs tab of the spreadsheet, and the
    dashboard's run detail. The last two are read by everyone the sheet is
    shared with.

    A canary rather than a pattern: a real password is whatever somebody chose,
    so the test looks for the exact value and would fail on any encoding of it.
    """

    CANARY = "canary-Pa55 w@rd/+="

    def test_a_network_failure_does_not_quote_the_password(self):
        store = remote_store.RemoteSheetsStore(
            "https://this-host-does-not-exist.invalid/exec", self.CANARY)
        # No retries: this is about what the message says, not about waiting.
        with unittest.mock.patch.object(remote_store, "RETRIES", 1):
            with self.assertRaises(remote_store.RemoteStoreError) as caught:
                store._request("GET", params={"action": "inputs",
                                              "password": store.password})
        self.assertNotIn(self.CANARY, str(caught.exception))
        self.assertIn("password=***", str(caught.exception))

    def test_nor_does_the_traceback_behind_it(self):
        """`raise ... from exc` would print the original, unredacted, in full."""
        store = remote_store.RemoteSheetsStore(
            "https://this-host-does-not-exist.invalid/exec", self.CANARY)
        with unittest.mock.patch.object(remote_store, "RETRIES", 1):
            try:
                store._request("GET", params={"action": "inputs",
                                              "password": store.password})
            except remote_store.RemoteStoreError as exc:
                rendered = "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__))
        self.assertNotIn(self.CANARY, rendered)

    def test_the_percent_encoded_form_counts_too(self):
        """A password in a URL is not the password as it was typed."""
        cleaned = remote_store.redact(
            "tried https://x/exec?action=rows&password=a%40b%20c", "a@b c")
        self.assertNotIn("a%40b%20c", cleaned)
        self.assertNotIn("a@b c", cleaned)

    def test_what_the_agent_writes_to_the_sheet_is_redacted(self):
        """A run summary is a spreadsheet cell, and cells are shared."""
        sent = {}

        class Store:
            password = self.CANARY

        class Fake(agent_module.Agent):
            def __init__(inner):                     # noqa: N805 - test double
                inner.store = Store()

            def _post(inner, action, **fields):      # noqa: N805
                sent.update(fields)
                return {}

        Fake().report("r1", status="failed",
                      summary=f"boom: https://x/exec?password={self.CANARY}")
        self.assertNotIn(self.CANARY, sent.get("summary", ""))


if __name__ == "__main__":
    unittest.main()
