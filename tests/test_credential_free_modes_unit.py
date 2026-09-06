"""What a machine holding only a project password can and cannot do.

The dashboard offers ten run modes and every one of them lands on somebody's
laptop, which has no Google key. Two rounds of this have now been got wrong the
same way, and neither was caught by a test:

  * every entry point opened the control spreadsheet before choosing a store,
    so all ten died with FileNotFoundError before doing any work;
  * four of them then reached for the key deeper in, and one of those is step 1
    of `full` — so the most-used button got nothing done at all.

Both survived because the machine they were written on HAS the key. The guard
in test_store_parity_unit.py looked for `GoogleSheetsStore(...)` being built
directly and found nothing, because these modules do not build one — they call
gspread themselves.

So this file asserts the two halves of the answer: the modes that work must not
ask for credentials, and the ones that genuinely cannot must say so by name
rather than by traceback.
"""

import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_store                                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CREDENTIAL_FREE = {
    "credentials": {"SETTINGS_WEB_APP_URL": "https://example.invalid/exec",
                    "PROJECT_PASSWORD": "not-used-here"},
}


class ANamedLimit(unittest.TestCase):
    """A step that cannot be done says which, and why."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, CREDENTIAL_FREE["credentials"])
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_enrichment_refuses_before_it_opens_anything(self):
        import company_enricher
        with self.assertRaises(remote_store.NeedsGoogleKey) as caught:
            company_enricher.enrich({"spreadsheet_id": "x"}, "Companies")
        said = str(caught.exception)
        # The reason, not just the refusal: it is the one step whose work has
        # no remote equivalent, and the reader has to be able to act on that.
        self.assertIn("hyperlink", said)
        self.assertIn("--skip-enrichment", said)

    def test_cleanup_refuses_the_same_way(self):
        import cleanup_validation
        with self.assertRaises(remote_store.NeedsGoogleKey):
            # Deliberately an empty config: what this machine can do does not
            # depend on how the file is filled in, so the refusal must come
            # before any of it is read.
            cleanup_validation.run_cleanup({})

    def test_it_is_not_an_ordinary_error(self):
        """`full` distinguishes the two, so they must be distinguishable."""
        self.assertTrue(issubclass(remote_store.NeedsGoogleKey, RuntimeError))


class TheFullRunKeepsGoing(unittest.TestCase):
    """Enrichment is step 1. Stopping there meant `full` did nothing at all."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, CREDENTIAL_FREE["credentials"])
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_steps_after_an_unavailable_one_still_run(self):
        import automation_pipeline
        ran = []

        def unavailable(config):
            raise remote_store.NeedsGoogleKey("needs the key")

        with mock.patch.object(automation_pipeline, "run_enrichment", unavailable), \
             mock.patch.object(automation_pipeline, "run_career_page_scraping",
                               lambda c: ran.append("career")), \
             mock.patch.object(automation_pipeline, "run_keyword_scraping",
                               lambda c: ran.append("keyword")), \
             mock.patch.object(automation_pipeline, "run_validation",
                               lambda c: ran.append("validation")):
            automation_pipeline.run_pipeline({})
        self.assertEqual(ran, ["career", "keyword", "validation"])

    def test_but_a_real_failure_still_stops_it(self):
        """Only "this machine cannot" is survivable. A broken step is not."""
        import automation_pipeline

        def broken(config):
            raise ValueError("the sheet is malformed")

        with mock.patch.object(automation_pipeline, "run_enrichment", broken), \
             mock.patch.object(automation_pipeline, "run_career_page_scraping",
                               lambda c: None):
            with self.assertRaises(SystemExit) as caught:
                automation_pipeline.run_pipeline({})
        self.assertEqual(caught.exception.code, 1)

    def test_and_it_says_what_it_left_out(self):
        """The dashboard shows the tail of the output as the run's summary, so
        a run that skipped a step must not read like one that did everything."""
        import automation_pipeline
        with mock.patch.object(automation_pipeline, "run_enrichment",
                               lambda c: (_ for _ in ()).throw(
                                   remote_store.NeedsGoogleKey("needs the key"))), \
             mock.patch.object(automation_pipeline, "run_career_page_scraping", lambda c: None), \
             mock.patch.object(automation_pipeline, "run_keyword_scraping", lambda c: None), \
             mock.patch.object(automation_pipeline, "run_validation", lambda c: None), \
             self.assertLogs("automation_pipeline", level="WARNING") as logged:
            automation_pipeline.run_pipeline({})
        last = logged.output[-1]
        self.assertIn("COMPLETE", last)
        self.assertIn("without enrichment", last)


class NoModuleReachesForAKeyUnasked(unittest.TestCase):
    """The guard the earlier one was missing.

    test_store_parity_unit.py forbids building a GoogleSheetsStore directly.
    These modules never did — they call google.oauth2 themselves, which that
    guard cannot see. So: any module that builds service-account credentials
    must also ask whether this machine is meant to be using them.
    """

    ENTRY_POINTS = ["main.py", "automation_pipeline.py", "job_validator.py",
                    "company_enricher.py", "company_classifier.py",
                    "company_validator.py", "pagination_analyzer.py",
                    "cleanup_validation.py", "settings_sheet.py"]

    def test_every_module_that_builds_credentials_checks_first(self):
        for name in self.ENTRY_POINTS:
            source = open(os.path.join(ROOT, name), encoding="utf-8").read()
            tree = ast.parse(source)
            builds = any(
                isinstance(node, ast.Attribute)
                and node.attr == "from_service_account_file"
                for node in ast.walk(tree)
            ) or "_build_credentials" in source
            if not builds:
                continue
            with self.subTest(module=name):
                self.assertIn(
                    "is_configured", source,
                    f"{name} builds Google credentials but never asks whether "
                    "this machine has only a project password. That is how "
                    "every run mode came to die on a missing key file.")


if __name__ == "__main__":
    unittest.main()
