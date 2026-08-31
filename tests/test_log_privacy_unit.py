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
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger_setup                                            # noqa: E402

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

    def test_both_jobs_raise_the_console_level(self):
        for name, job in self.doc["jobs"].items():
            with self.subTest(job=name):
                self.assertEqual((job.get("env") or {}).get("LOG_CONSOLE_LEVEL"),
                                 "warning")


if __name__ == "__main__":
    unittest.main()
